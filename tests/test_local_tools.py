from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import time
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest
from click.testing import CliRunner

from openagent_cli.client import GatewayClient
from openagent_cli import legacy_main as cli_main
from openagent_host_tools import HostPaths
from openagent_host_tools.idempotency import IdempotencyLedger
from openagent_host_tools.local_broker import (
    LocalBrokerClient,
    LocalBrokerServer,
    LocalCapabilityClient,
)


class _ChatWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, value: dict) -> None:
        self.sent.append(value)


@pytest.mark.asyncio
async def test_session_open_carries_client_instance_id():
    client = GatewayClient("ws://127.0.0.1:1234/ws")
    ws = _ChatWS()
    client._ws = ws
    await client.send_session_open("session-1")
    assert ws.sent[0]["type"] == "session_open"
    assert ws.sent[0]["client_instance_id"] == client.client_instance_id


class _FakeLocalHost:
    def __init__(self):
        self.closed = False
        self.released = []
        self.disconnect_sinks = set()

    async def start(self):
        return None

    async def status(self):
        return {"consent": {"enabled": True}}

    async def catalog(self):
        return [
            {
                "name": "filesystem",
                "version": "1",
                "instructions": "client paths",
                "tools": [],
            }
        ]

    async def release_principal(self, principal):
        self.released.append(principal)

    async def close(self):
        self.closed = True

    def subscribe_disconnect(self, sink):
        self.disconnect_sinks.add(sink)

    def unsubscribe_disconnect(self, sink):
        self.disconnect_sinks.discard(sink)

    async def trigger_disconnect(self):
        for sink in list(self.disconnect_sinks):
            result = sink()
            if asyncio.iscoroutine(result):
                await result


class _FakeDialer:
    def __init__(self, account_id: str, device_id: str):
        self.cert = SimpleNamespace(
            network_id=account_id,
            device_pubkey_hex=device_id,
        )

    def parsed_cert(self):
        return self.cert

    async def close(self):
        return None


class _CapabilityWS:
    def __init__(self, instance_id: str, account_id: str, device_id: str):
        self.instance_id = instance_id
        self.account_id = account_id
        self.device_id = device_id
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, value: dict):
        self.sent.append(value)

    async def receive(self, timeout=None):
        return SimpleNamespace(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps(
                {
                    "type": "capability_hello_ack",
                    "protocol": "client-capabilities/1",
                    "accepted": True,
                    "device_id": self.device_id,
                    "account_id": self.account_id,
                    "client_instance_id": self.instance_id,
                    "generation": self.sent[0]["generation"],
                }
            ),
        )

    def __aiter__(self):
        async def empty():
            if False:
                yield None

        return empty()

    async def close(self):
        self.closed = True


class _BlockingCapabilityWS(_CapabilityWS):
    def __init__(
        self,
        instance_id: str,
        account_id: str,
        device_id: str,
        *,
        close_gate=None,
    ):
        super().__init__(instance_id, account_id, device_id)
        self.frames = asyncio.Queue()
        self.close_gate = close_gate
        self.close_started = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def close(self, *args, **kwargs):
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        if not self.closed:
            self.closed = True
            await self.frames.put(None)


class _Session:
    def __init__(self, ws):
        self.ws = ws
        self.urls = []

    async def ws_connect(self, url):
        self.urls.append(url)
        return self.ws


class _RotatingSession:
    def __init__(self, sockets):
        self.sockets = list(sockets)
        self.urls = []

    async def ws_connect(self, url):
        self.urls.append(url)
        if not self.sockets:
            raise ConnectionError("no more fake sockets")
        return self.sockets.pop(0)


@pytest.mark.asyncio
async def test_capability_socket_uses_same_instance_and_dedicated_path(monkeypatch):
    import openagent_host_tools

    host = _FakeLocalHost()
    monkeypatch.setattr(openagent_host_tools, "LocalCapabilityClient", lambda: host)
    client = GatewayClient(
        "ws://127.0.0.1:9999/ws",
        dialer=_FakeDialer("account-1", "device-1"),
    )
    capability_ws = _CapabilityWS(client.client_instance_id, "account-1", "device-1")
    session = _Session(capability_ws)
    client._session = session

    await client._start_local_capabilities()
    try:
        assert session.urls == ["ws://127.0.0.1:9999/ws/capabilities"]
        hello = capability_ws.sent[0]
        assert hello["type"] == "capability_hello"
        assert hello["client_instance_id"] == client.client_instance_id
        assert hello["servers"][0]["name"] == "filesystem"
    finally:
        await client._stop_local_capabilities()
    assert host.closed is True


@pytest.mark.asyncio
async def test_new_cli_keeps_chat_working_when_old_server_has_no_capability_endpoint(
    monkeypatch,
):
    """A server predating /ws/capabilities must remain a usable chat peer."""

    import openagent_host_tools

    host = _FakeLocalHost()
    monkeypatch.setattr(openagent_host_tools, "LocalCapabilityClient", lambda: host)
    capability_attempts = []
    session_open = asyncio.get_running_loop().create_future()

    @web.middleware
    async def observe_missing_capability_endpoint(request, handler):
        if request.path == "/ws/capabilities":
            capability_attempts.append(request.path)
        return await handler(request)

    async def legacy_chat(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        auth = await ws.receive_json()
        assert auth["type"] == "auth"
        await ws.send_json(
            {
                "type": "auth_ok",
                "agent_name": "old-server-agent",
                "version": "legacy",
                "handle": "legacy-agent",
                "network": "Legacy Network",
                "network_id": "legacy-network",
            }
        )
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(message.data)
            if frame.get("type") == "session_open" and not session_open.done():
                session_open.set_result(frame)
        return ws

    app = web.Application(middlewares=[observe_missing_capability_endpoint])
    app.router.add_get("/ws", legacy_chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = GatewayClient(
        f"ws://127.0.0.1:{port}/ws",
        dialer=_FakeDialer("legacy-network", "legacy-device"),
    )
    try:
        await client.connect()
        assert client.is_connected is True
        assert capability_attempts == ["/ws/capabilities"]
        assert host.closed is True

        await client.send_session_open("legacy-compatible-session")
        frame = await asyncio.wait_for(session_open, timeout=3)
        assert frame["session_id"] == "legacy-compatible-session"
        assert frame["client_instance_id"] == client.client_instance_id
        assert client.is_connected is True
    finally:
        await client.disconnect()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_capability_socket_reconnects_same_execution_host(monkeypatch):
    import asyncio
    import openagent_host_tools

    host = _FakeLocalHost()
    monkeypatch.setattr(openagent_host_tools, "LocalCapabilityClient", lambda: host)
    client = GatewayClient(
        "ws://127.0.0.1:9999/ws",
        dialer=_FakeDialer("account-1", "device-1"),
    )
    client._ws = _ChatWS()
    first = _CapabilityWS(client.client_instance_id, "account-1", "device-1")
    second = _CapabilityWS(client.client_instance_id, "account-1", "device-1")
    session = _RotatingSession([first, second])
    client._session = session

    await client._start_local_capabilities()
    for _ in range(200):
        if len(session.urls) >= 2:
            break
        await asyncio.sleep(0.01)
    try:
        assert len(session.urls) >= 2
        assert first.sent[0]["client_instance_id"] == second.sent[0]["client_instance_id"]
        assert first.sent[0]["generation"] == second.sent[0]["generation"]
        assert host.released == []
    finally:
        await client._stop_local_capabilities()


@pytest.mark.asyncio
async def test_host_transport_loss_closes_exact_capability_socket_without_listener_cycle(
    monkeypatch,
):
    import openagent_host_tools

    host = _FakeLocalHost()
    monkeypatch.setattr(openagent_host_tools, "LocalCapabilityClient", lambda: host)
    client = GatewayClient(
        "ws://127.0.0.1:9999/ws",
        dialer=_FakeDialer("account-1", "device-1"),
    )
    client._ws = _ChatWS()
    close_gate = asyncio.Event()
    first = _BlockingCapabilityWS(
        client.client_instance_id,
        "account-1",
        "device-1",
        close_gate=close_gate,
    )
    second = _BlockingCapabilityWS(
        client.client_instance_id,
        "account-1",
        "device-1",
    )
    session = _RotatingSession([first, second])
    client._session = session

    await client._start_local_capabilities()
    try:
        # The broker listener awaits bridge._on_host_disconnect. It must return
        # even while aiohttp WS close is blocked, otherwise teardown cycles on
        # the listener that reported the loss.
        await asyncio.wait_for(host.trigger_disconnect(), timeout=0.2)
        await asyncio.wait_for(first.close_started.wait(), timeout=1)
        assert len(session.urls) == 1
        assert client._capability_generation == 1
        close_gate.set()
        for _ in range(200):
            if len(session.urls) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(session.urls) == 2
        assert first.closed is True
        assert first.sent[0]["generation"] == second.sent[0]["generation"] == 1
        assert first.sent[0]["client_instance_id"] == second.sent[0]["client_instance_id"]
        assert host.released == []
    finally:
        close_gate.set()
        await client._stop_local_capabilities()


def test_local_tools_enable_and_status_commands(monkeypatch):
    class FakeControl:
        enabled = False

        async def set_consent(self, enabled):
            self.enabled = enabled

        async def status(self):
            return {
                "consent": {"enabled": self.enabled},
                "consent_path": "/tmp/client-tools-consent.json",
                "config_path": "/tmp/client-mcps.toml",
                "servers": [
                    {
                        "name": "filesystem",
                        "source": "builtin",
                        "available": True,
                        "tools": [{"name": "read_text_file"}],
                    }
                ],
            }

        async def close(self):
            return None

    fake = FakeControl()

    async def open_fake():
        return fake

    monkeypatch.setattr(cli_main, "_open_local_tools_client", open_fake)
    runner = CliRunner()
    enabled = runner.invoke(cli_main.cli, ["local-tools", "enable", "--yes"])
    assert enabled.exit_code == 0, enabled.output
    assert "enabled persistently" in enabled.output
    status = runner.invoke(cli_main.cli, ["local-tools", "status"])
    assert status.exit_code == 0, status.output
    assert "filesystem" in status.output


def test_frozen_cli_dispatches_private_broker_mode(monkeypatch):
    import openagent_host_tools.stdio

    called = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["openagent-cli", "--broker"])
    monkeypatch.setattr(openagent_host_tools.stdio, "main", lambda: called.append(True))
    cli_main.main()
    assert called == [True]


@pytest.mark.asyncio
async def test_cli_capability_socket_real_broker_gateway_roundtrip(tmp_path, monkeypatch):
    import asyncio

    paths = HostPaths.discover(tmp_path / "user")
    monkeypatch.setenv("OPENAGENT_HOST_TOOLS_HOME", str(paths.home))
    broker = LocalBrokerServer(paths)
    broker_task = asyncio.create_task(broker.run())
    for _ in range(200):
        if broker.unix_socket_path.exists():
            break
        await asyncio.sleep(0.01)
    control = LocalCapabilityClient(paths)
    await control.start()
    await control.set_consent(True)

    result_future = asyncio.get_running_loop().create_future()
    hello_frames = []

    async def chat(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        auth = await ws.receive_json()
        assert auth["type"] == "auth"
        await ws.send_json(
            {
                "type": "auth_ok",
                "agent_name": "test-agent",
                "version": "test",
                "handle": "tester",
                "network": "Test Network",
                "network_id": "network-e2e",
            }
        )
        async for _message in ws:
            pass
        return ws

    async def capabilities(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            frame = json.loads(message.data)
            if frame.get("type") == "capability_hello":
                hello_frames.append(frame)
                await ws.send_json(
                    {
                        "type": "capability_hello_ack",
                        "protocol": "client-capabilities/1",
                        "accepted": True,
                        "device_id": "device-e2e",
                        "account_id": "network-e2e",
                        "client_instance_id": frame["client_instance_id"],
                        "generation": frame["generation"],
                    }
                )
                args = {"path": str(tmp_path)}
                await ws.send_json(
                    {
                        "type": "client_tool_call",
                        "call_id": "gateway-e2e",
                        "generation": frame["generation"],
                        "server": "filesystem",
                        "tool": "list_directory",
                        "args": args,
                        "account_id": "network-e2e",
                        "arguments_sha256": IdempotencyLedger.arguments_sha256(args),
                    }
                )
            elif frame.get("type") == "client_tool_result" and frame.get(
                "call_id"
            ) == "gateway-e2e":
                if not result_future.done():
                    result_future.set_result(frame)
        return ws

    app = web.Application()
    app.router.add_get("/ws", chat)
    app.router.add_get("/ws/capabilities", capabilities)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = GatewayClient(
        f"ws://127.0.0.1:{port}/ws",
        dialer=_FakeDialer("network-e2e", "device-e2e"),
    )
    try:
        await client.connect()
        result = await asyncio.wait_for(result_future, timeout=5)
        assert client.network_id == "network-e2e"
        assert client.network_name == "Test Network"
        assert hello_frames[0]["client_instance_id"] == client.client_instance_id
        assert result["result"]["_meta"]["openagent/location"] == "client"
        assert result["result"]["_meta"]["openagent/pathSemantics"] == "client-local"
    finally:
        await client.disconnect()
        await control.close()
        await runner.cleanup()
        broker_task.cancel()
        await asyncio.gather(broker_task, return_exceptions=True)


@pytest.mark.skipif(os.name == "nt", reason="Unix broker SIGKILL/restart regression")
@pytest.mark.asyncio
async def test_cli_broker_loss_reconnects_same_generation_and_retries_safe_call(
    tmp_path,
    monkeypatch,
):
    """Exercise broker death -> capability WS loss -> exact safe retry end to end."""

    from openagent_host_tools.host import _principal_id

    paths = HostPaths.discover(tmp_path / "user")
    monkeypatch.setenv("OPENAGENT_HOST_TOOLS_HOME", str(paths.home))
    env = os.environ.copy()

    async def spawn_broker():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "openagent_host_tools",
            "--broker",
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(300):
            probe = LocalBrokerClient(paths)
            try:
                await probe.connect()
            except (OSError, EOFError, ConnectionRefusedError, FileNotFoundError):
                await probe.close()
                await asyncio.sleep(0.01)
                continue
            await probe.close()
            return process
        process.kill()
        await process.wait()
        raise AssertionError("broker did not accept connections")

    first_broker = await spawn_broker()
    second_broker = None
    socket_path = LocalBrokerServer(paths).unix_socket_path
    control = LocalCapabilityClient(paths)
    await control.start()
    await control.set_consent(True)

    target = tmp_path / "retry.txt"
    target.write_text("safe-retry-ok", encoding="utf-8")
    args = {"path": str(target)}
    call_id = "safe-call-after-broker-loss"
    mutation_id = "mutation-in-flight-at-broker-loss"
    mutation_marker = tmp_path / "mutation-effect"
    mutation_args = {
        "command": shlex.join(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    f"Path({str(mutation_marker)!r}).write_text('effect'); "
                    "sys.stdin.read()"
                ),
            ]
        )
    }
    result_future = asyncio.get_running_loop().create_future()
    first_hello = asyncio.Event()
    hello_frames = []
    retry_sent_at = 0.0
    connection_number = 0
    mutation_results = []

    async def chat(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        auth = await ws.receive_json()
        assert auth["type"] == "auth"
        await ws.send_json(
            {
                "type": "auth_ok",
                "agent_name": "test-agent",
                "version": "test",
                "handle": "tester",
                "network": "Test Network",
                "network_id": "network-retry",
            }
        )
        async for _message in ws:
            pass
        return ws

    async def capabilities(request):
        nonlocal connection_number, retry_sent_at
        connection_number += 1
        this_connection = connection_number
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            frame = json.loads(message.data)
            if frame.get("type") == "capability_hello":
                hello_frames.append(frame)
                await ws.send_json(
                    {
                        "type": "capability_hello_ack",
                        "protocol": "client-capabilities/1",
                        "accepted": True,
                        "device_id": "device-retry",
                        "account_id": "network-retry",
                        "client_instance_id": frame["client_instance_id"],
                        "generation": frame["generation"],
                    }
                )
                if this_connection == 1:
                    await ws.send_json(
                        {
                            "type": "client_tool_call",
                            "call_id": mutation_id,
                            "idempotency_key": mutation_id,
                            "generation": frame["generation"],
                            "server": "shell",
                            "tool": "shell_exec",
                            "args": mutation_args,
                            "account_id": "network-retry",
                            "arguments_sha256": IdempotencyLedger.arguments_sha256(
                                mutation_args
                            ),
                        }
                    )
                    first_hello.set()
                elif this_connection == 2:
                    retry_sent_at = time.monotonic()
                    await ws.send_json(
                        {
                            "type": "client_tool_call",
                            "call_id": call_id,
                            "idempotency_key": call_id,
                            "generation": frame["generation"],
                            "server": "filesystem",
                            "tool": "read_text_file",
                            "args": args,
                            "account_id": "network-retry",
                            "arguments_sha256": IdempotencyLedger.arguments_sha256(args),
                        }
                    )
            elif frame.get("type") == "client_tool_result":
                if frame.get("call_id") == mutation_id:
                    mutation_results.append(frame)
                elif frame.get("call_id") == call_id and not result_future.done():
                    result_future.set_result(frame)
        return ws

    app = web.Application()
    app.router.add_get("/ws", chat)
    app.router.add_get("/ws/capabilities", capabilities)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = GatewayClient(
        f"ws://127.0.0.1:{port}/ws",
        dialer=_FakeDialer("network-retry", "device-retry"),
    )
    try:
        await client.connect()
        await asyncio.wait_for(first_hello.wait(), timeout=3)
        for _ in range(300):
            if mutation_marker.exists():
                break
            await asyncio.sleep(0.01)
        assert mutation_marker.read_text(encoding="utf-8") == "effect"
        hello = hello_frames[0]
        principal = _principal_id(
            {
                "kind": "interactive-client",
                "client_instance_id": hello["client_instance_id"],
                "device_label": hello["device_label"],
                "account_id": "network-retry",
                "device_id": "device-retry",
                "generation": hello["generation"],
            }
        )
        crashed_owner = IdempotencyLedger(paths.state_db, inflight_lease_seconds=30)
        await crashed_owner.claim(
            principal,
            call_id,
            server="filesystem",
            tool="read_text_file",
            args=args,
            retry_stale=True,
        )

        first_broker.kill()
        await first_broker.wait()
        second_broker = await spawn_broker()
        result = await asyncio.wait_for(result_future, timeout=5)
        assert result.get("error") is None
        assert result["result"]["content"][0]["text"] == "safe-retry-ok"
        assert time.monotonic() - retry_sent_at < 1
        assert len(hello_frames) >= 2
        assert hello_frames[0]["generation"] == hello_frames[1]["generation"] == 1
        assert hello_frames[0]["client_instance_id"] == hello_frames[1]["client_instance_id"]
        # The mutation took effect but the broker died before returning. The
        # CLI must close the capability socket without a determinate result;
        # Gateway disconnect handling is what marks it indeterminate.
        assert mutation_results == []
        assert client.is_connected is True
    finally:
        await client.disconnect()
        await control.close()
        await runner.cleanup()
        if first_broker.returncode is None:
            first_broker.terminate()
            try:
                await asyncio.wait_for(first_broker.wait(), timeout=3)
            except asyncio.TimeoutError:
                first_broker.kill()
                await first_broker.wait()
        if second_broker is not None and second_broker.returncode is None:
            second_broker.terminate()
            try:
                await asyncio.wait_for(second_broker.wait(), timeout=3)
            except asyncio.TimeoutError:
                second_broker.kill()
                await second_broker.wait()
        stale_socket = socket_path.exists()
        socket_path.unlink(missing_ok=True)
    assert not stale_socket, "graceful replacement broker shutdown left a Unix socket"
