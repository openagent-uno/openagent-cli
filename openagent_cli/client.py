"""WebSocket + REST client for the OpenAgent Gateway over Iroh.

The legacy ``GatewayClient(url, token)`` constructor is preserved for
introspection / tests, but new code should use ``GatewayClient.from_network``
which performs the full ``handle@network`` → device-cert → loopback
proxy → aiohttp wiring.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import aiohttp

from openagent.gateway import protocol as P
from openagent.network import user_store
from openagent.network.client.session import LoopbackProxy, SessionDialer
from openagent.network.identity import (
    Identity,
    load_or_create_identity,
)
from openagent.network.iroh_node import IrohNode
from openagent.stream.collector import StreamCollector, fold_outbound_event
from openagent.stream.events import (
    AudioChunk, Interrupt, SessionClose, SessionOpen, TextFinal, now_ms,
)
from openagent.stream.wire import event_to_wire, wire_to_event

logger = logging.getLogger(__name__)


class GatewayClient:
    """Async WebSocket client to an OpenAgent Gateway."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        *,
        proxy: LoopbackProxy | None = None,
        node: IrohNode | None = None,
        dialer: SessionDialer | None = None,
        target_handle: str | None = None,
    ):
        # Two construction paths: ``url`` for raw debugging /
        # in-process tests, or the keyword bundle (proxy/node/dialer)
        # produced by ``from_network``. Exactly one is required.
        if url is None and proxy is None:
            raise ValueError("GatewayClient needs either url= or proxy=")
        self.url = url or proxy.ws_url
        self.token = token  # legacy debugging only — ignored over Iroh transport
        self._proxy = proxy
        self._node = node
        self._dialer = dialer
        self.target_handle = target_handle

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        # ``_stream_pending``: in-flight collectors keyed by session_id;
        # the listener folds outbound events into them via
        # ``fold_outbound_event`` and ``send_message`` awaits ``done``.
        # ``_opened_sessions``: sessions already ``session_open``'d on
        # this WS — cleared on disconnect since the gateway tears down
        # server-side ``StreamSession``s when the WS drops.
        self._stream_pending: dict[str, StreamCollector] = {}
        self._command_future: asyncio.Future | None = None
        self._opened_sessions: set[str] = set()
        self._status_cb: dict[str, Callable] = {}
        self._listener_task: asyncio.Task | None = None
        self.agent_name: str | None = None
        self.agent_version: str | None = None
        self.agent_handle: str | None = None
        self.network_id: str | None = None

    @classmethod
    async def from_network(
        cls,
        *,
        handle: str,
        network_name: str,
        password: str | None = None,
        invite_code: str | None = None,
        target_agent_handle: str | None = None,
    ) -> "GatewayClient":
        """Build an authed client for ``handle@network_name``.

        - Finds the network in the user store; raises ``LookupError``
          if it isn't there (caller is expected to register first via
          ``register_with_network``).
        - Refreshes the cert if expired (requires *password*).
        - Resolves the target agent's NodeId (defaults to the first
          agent in the network).
        - Spins up an Iroh node + a loopback proxy and returns a
          GatewayClient bound to the proxy URL.
        """
        from openagent.network.client.login import list_agents as coord_list_agents
        from openagent.network.client.login import refresh_cert
        from openagent.network.auth.device_cert import verify_cert
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        store = user_store.load()
        net = user_store.find(store, network_name)
        if net is None:
            raise LookupError(
                f"network {network_name!r} not in user store; run `openagent-cli connect "
                f"{handle}@{network_name} --invite <code>` first",
            )
        if net.handle != handle:
            raise LookupError(
                f"network {network_name!r} is bound to handle {net.handle!r}, not {handle!r}",
            )

        user_store.ensure_user_identity_dir()
        device_identity = load_or_create_identity(user_store.user_identity_path())

        node = IrohNode(device_identity)

        # Cert: load from disk; refresh if missing/expired.
        cert_wire = user_store.read_cert(net)
        cert_valid = False
        if cert_wire:
            try:
                pubkey = Ed25519PublicKey.from_public_bytes(net.coordinator_pubkey_bytes)
                verify_cert(
                    cert_wire,
                    coordinator_pubkey=pubkey,
                    expected_network_id=net.network_id,
                )
                cert_valid = True
            except Exception:
                cert_valid = False

        if not cert_valid:
            if password is None:
                raise PermissionError(
                    f"cert for {handle}@{network_name} is missing or expired; supply password=",
                )
            await node.start()
            try:
                cert_wire = await refresh_cert(
                    node=node,
                    coordinator_node_id=net.coordinator_node_id,
                    coordinator_pubkey_bytes=net.coordinator_pubkey_bytes,
                    handle=handle,
                    password=password,
                    device_identity=device_identity,
                    network_id=net.network_id,
                )
                user_store.write_cert(net, cert_wire)
                from openagent.network.user_store import save
                import time as _time
                net.last_login_at = _time.time()
                save(store)
            except Exception:
                await node.stop()
                raise
        else:
            await node.start()

        from openagent.network.client.session import NetworkBinding
        binding = NetworkBinding(
            network_id=net.network_id,
            network_name=net.name,
            coordinator_node_id=net.coordinator_node_id,
            coordinator_pubkey_bytes=net.coordinator_pubkey_bytes,
            our_handle=handle,
        )
        dialer = SessionDialer(node=node, binding=binding, cert_wire=cert_wire)

        # Resolve target agent: explicit handle wins; otherwise pick
        # the first registered agent in the network. The user can
        # override later with ``openagent-cli use <handle>``.
        agents = await coord_list_agents(node=node, coordinator_node_id=net.coordinator_node_id)
        if not agents:
            await node.stop()
            raise LookupError(f"no agents registered in network {network_name!r}")

        chosen = None
        if target_agent_handle:
            chosen = next((a for a in agents if a.get("handle") == target_agent_handle), None)
        elif store.active_agent:
            chosen = next((a for a in agents if a.get("handle") == store.active_agent), None)
        if chosen is None:
            chosen = agents[0]
        target_node_id = chosen["node_id"]
        target_handle = chosen["handle"]

        proxy = LoopbackProxy(dialer=dialer, target_node_id=target_node_id)
        await proxy.start()

        return cls(
            proxy=proxy,
            node=node,
            dialer=dialer,
            target_handle=target_handle,
        )

    @property
    def base_url(self) -> str:
        return self.url.replace("ws://", "http://").replace("/ws", "")

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.url)
        # Legacy AUTH frame is ignored by the new gateway, but sending
        # it costs nothing and keeps wire compatibility tests passing.
        await self._ws.send_json({"type": P.AUTH, "client_id": "cli"})
        resp = await self._ws.receive_json()
        if resp.get("type") == P.AUTH_ERROR:
            raise ConnectionError(f"Auth failed: {resp.get('reason')}")
        self.agent_name = resp.get("agent_name")
        self.agent_version = resp.get("version")
        self.agent_handle = resp.get("handle")
        self.network_id = resp.get("network")
        self._listener_task = asyncio.create_task(self._listen())

    async def disconnect(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        if self._proxy is not None:
            try:
                await self._proxy.stop()
            except Exception:
                pass
        if self._dialer is not None:
            try:
                await self._dialer.close()
            except Exception:
                pass
        if self._node is not None:
            try:
                await self._node.stop()
            except Exception:
                pass
        self._opened_sessions.clear()
        self._stream_pending.clear()

    async def _listen(self) -> None:
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            t = data.get("type")
            sid = data.get("session_id")
            collector = self._stream_pending.get(sid) if sid else None

            if t == P.STATUS:
                cb = self._status_cb.get(sid)
                if cb is not None:
                    await cb(data.get("text", ""))
                continue
            if t == P.COMMAND_RESULT:
                if self._command_future is not None and not self._command_future.done():
                    self._command_future.set_result(data)
                    self._command_future = None
                continue
            if t == P.ERROR and collector is None:
                logger.warning("gateway error (no session): %s", data.get("text"))
                continue

            evt = wire_to_event(data)
            if evt is None or collector is None:
                continue
            if fold_outbound_event(collector, evt):
                collector.done.set()

    async def send_message(
        self,
        text: str,
        session_id: str,
        on_status: Callable | None = None,
        *,
        source: str = "user_typed",
    ) -> dict:
        """Push a typed message into the user's stream session and await the reply."""
        if session_id not in self._opened_sessions:
            await self._ws.send_json(event_to_wire(SessionOpen(
                session_id=session_id,
                ts_ms=now_ms(),
                profile="batched",
                client_kind="cli",
                speak=False,
            )))
            self._opened_sessions.add(session_id)

        collector = StreamCollector()
        self._stream_pending[session_id] = collector
        if on_status:
            self._status_cb[session_id] = on_status

        try:
            await self._ws.send_json(event_to_wire(TextFinal(
                session_id=session_id,
                ts_ms=now_ms(),
                text=text,
                source=source,  # type: ignore[arg-type]
            )))
        except Exception:
            self._stream_pending.pop(session_id, None)
            self._status_cb.pop(session_id, None)
            raise

        try:
            await collector.done.wait()
        finally:
            self._stream_pending.pop(session_id, None)
            self._status_cb.pop(session_id, None)

        return collector.to_legacy_reply()

    async def send_command(self, name: str) -> str:
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._command_future = fut
        try:
            await self._ws.send_json({"type": P.COMMAND, "name": name})
            result = await fut
        finally:
            if self._command_future is fut:
                self._command_future = None
        return result.get("text", "")

    # ── Stream protocol helpers (opt-in) ────────────────────────────

    async def send_session_open(
        self,
        session_id: str,
        *,
        profile: str = "realtime",
        language: str | None = None,
        client_kind: str | None = "cli",
    ) -> None:
        await self._ws.send_json(event_to_wire(SessionOpen(
            session_id=session_id, ts_ms=now_ms(),
            profile=profile,  # type: ignore[arg-type]
            language=language,
            client_kind=client_kind,
        )))

    async def send_session_close(self, session_id: str) -> None:
        await self._ws.send_json(event_to_wire(SessionClose(
            session_id=session_id, ts_ms=now_ms(),
        )))

    async def send_text_final(
        self, session_id: str, text: str, *, source: str = "user_typed"
    ) -> None:
        await self._ws.send_json(event_to_wire(TextFinal(
            session_id=session_id, ts_ms=now_ms(),
            text=text, source=source,  # type: ignore[arg-type]
        )))

    async def send_audio_chunk_in(
        self,
        session_id: str,
        data: bytes,
        *,
        end_of_speech: bool = False,
        sample_rate: int | None = None,
        encoding: str | None = None,
    ) -> None:
        await self._ws.send_json(event_to_wire(AudioChunk(
            session_id=session_id, ts_ms=now_ms(),
            data=data, end_of_speech=end_of_speech,
            sample_rate=sample_rate or 0, encoding=encoding or "",
        )))

    async def send_interrupt(
        self, session_id: str, *, reason: str = "manual"
    ) -> None:
        await self._ws.send_json(event_to_wire(Interrupt(
            session_id=session_id, ts_ms=now_ms(),
            reason=reason,  # type: ignore[arg-type]
        )))

    # REST helpers
    async def rest_get(self, path: str) -> dict:
        async with self._session.get(f"{self.base_url}{path}") as r:
            return await r.json()

    async def rest_patch(self, path: str, data) -> dict:
        async with self._session.patch(f"{self.base_url}{path}", json=data) as r:
            return await r.json()

    async def rest_put(self, path: str, data) -> dict:
        async with self._session.put(f"{self.base_url}{path}", json=data) as r:
            return await r.json()

    async def rest_post(self, path: str, data=None) -> dict:
        async with self._session.post(f"{self.base_url}{path}", json=data if data is not None else {}) as r:
            return await r.json()

    async def rest_delete(self, path: str) -> dict:
        async with self._session.delete(f"{self.base_url}{path}") as r:
            try:
                return await r.json()
            except Exception:
                return {"ok": r.status < 400}

    async def download_file(self, remote_path: str, dest_path: str) -> int:
        """Fetch a file off the agent's filesystem via ``/api/files``.

        Auth is carried over the Iroh transport's cert prefix — no
        token query parameter is appended anymore.
        """
        async with self._session.get(f"{self.base_url}/api/files", params={"path": remote_path}) as r:
            if r.status != 200:
                body = await r.text()
                raise RuntimeError(f"{r.status} {body[:200]}")
            total = 0
            with open(dest_path, "wb") as f:
                async for chunk in r.content.iter_chunked(64 * 1024):
                    f.write(chunk)
                    total += len(chunk)
            return total
