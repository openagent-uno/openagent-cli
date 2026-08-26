from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp

from openagent_cli.client import GatewayClient, _StreamCollector


class _WebSocket:
    def __init__(self, auth_reply=None, frames=()):
        self.closed = False
        self.auth_reply = auth_reply or {"type": "auth_ok"}
        self.frames = list(frames)
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_json(self):
        return self.auth_reply

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


class _Session:
    def __init__(self, ws=None, block=False):
        self.ws = ws
        self.block = block
        self.started = asyncio.Event()
        self.closed = False

    async def ws_connect(self, _url):
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        return self.ws

    async def close(self):
        self.closed = True


class _Node:
    def __init__(self):
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class _Dialer:
    def __init__(self, **_kwargs):
        self.closed = False

    async def close(self):
        self.closed = True


class GatewayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_from_network_cancellation_closes_node_and_dialer(self):
        node = _Node()
        dialer = _Dialer()
        lookup_started = asyncio.Event()

        async def blocked_agent_lookup(**_kwargs):
            lookup_started.set()
            await asyncio.Event().wait()

        network = SimpleNamespace(
            network_id="network-1",
            name="test-network",
            handle="alice",
            coordinator_node_id="coordinator-node",
            coordinator_pubkey_bytes=b"x" * 32,
        )
        store = SimpleNamespace(active_agent=None)
        with (
            patch("openagent_cli.client.user_store.load", return_value=store),
            patch("openagent_cli.client.user_store.find", return_value=network),
            patch("openagent_cli.client.user_store.ensure_user_identity_dir"),
            patch("openagent_cli.client.user_store.user_identity_path", return_value="identity"),
            patch("openagent_cli.client.user_store.read_cert", return_value=b"cert"),
            patch("openagent_cli.client.load_or_create_identity", return_value=object()),
            patch("openagent_cli.client.IrohNode", return_value=node),
            patch("openagent_cli.client.SessionDialer", return_value=dialer),
            patch("openagent_cli.network.auth.device_cert.verify_cert", return_value=object()),
            patch(
                "cryptography.hazmat.primitives.asymmetric.ed25519."
                "Ed25519PublicKey.from_public_bytes",
                return_value=object(),
            ),
            patch(
                "openagent_cli.network.client.login.list_agents",
                side_effect=blocked_agent_lookup,
            ),
        ):
            task = asyncio.create_task(GatewayClient.from_network(
                handle="alice", network_name="test-network",
            ))
            await lookup_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(node.started)
        self.assertTrue(node.stopped)
        self.assertTrue(dialer.closed)

    async def test_auth_failure_closes_websocket_and_session(self):
        ws = _WebSocket({"type": "auth_error", "reason": "private input"})
        session = _Session(ws)
        client = GatewayClient(url="ws://127.0.0.1:1/ws")
        with patch("openagent_cli.client.aiohttp.ClientSession", return_value=session):
            with self.assertRaisesRegex(ConnectionError, "authentication failed"):
                await client.connect()
        self.assertTrue(ws.closed)
        self.assertTrue(session.closed)

    async def test_connect_cancellation_closes_session(self):
        session = _Session(block=True)
        client = GatewayClient(url="ws://127.0.0.1:1/ws")
        with patch("openagent_cli.client.aiohttp.ClientSession", return_value=session):
            task = asyncio.create_task(client.connect())
            await session.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(session.closed)

    async def test_listener_protocol_failure_releases_pending_turn(self):
        bad_frame = aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, "not-json", "")
        client = GatewayClient(url="ws://127.0.0.1:1/ws")
        client._ws = _WebSocket(frames=(bad_frame,))  # type: ignore[assignment]
        collector = _StreamCollector()
        client._stream_pending["session-1"] = collector
        client._listener_task = asyncio.current_task()

        await client._listen()

        self.assertTrue(collector.done.is_set())
        self.assertTrue(collector.errored)
        self.assertEqual(collector.error_text, "Gateway connection closed")

    async def test_gateway_error_text_is_not_logged(self):
        frame = aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps({
            "type": "error", "text": "database locked private query",
        }), "")
        client = GatewayClient(url="ws://127.0.0.1:1/ws")
        client._ws = _WebSocket(frames=(frame,))  # type: ignore[assignment]
        with self.assertLogs("openagent_cli.client", level="WARNING") as captured:
            await client._listen()
        self.assertNotIn("database locked", "\n".join(captured.output))
