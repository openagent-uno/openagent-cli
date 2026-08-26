"""WebSocket + REST client for the OpenAgent Gateway over Iroh.

The legacy ``GatewayClient(url, token)`` constructor is preserved for
introspection / tests, but new code should use ``GatewayClient.from_network``
which performs the full ``handle@network`` → device-cert → loopback
proxy → aiohttp wiring.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import aiohttp

from .network import user_store
from .network.client.session import LoopbackProxy, SessionDialer
from .network.identity import (
    Identity,
    load_or_create_identity,
)
from .network.iroh_node import IrohNode

logger = logging.getLogger(__name__)

# Gateway + terminal protocol frame types.  String literals are the stable
# wire contract; importing the server's command/runtime package here would
# make the standalone wheel depend on an OpenAgent server installation.
_P_AUTH = "auth"
_P_AUTH_ERROR = "auth_error"
_P_COMMAND = "command"
_P_COMMAND_RESULT = "command_result"
_P_STATUS = "status"
_P_ERROR = "error"
_P_SESSION_COMPACTED = "session_compacted"

TERMINAL_OPEN = "terminal_open"
TERMINAL_INPUT = "terminal_input"
TERMINAL_RESIZE = "terminal_resize"
TERMINAL_SIGNAL = "terminal_signal"
TERMINAL_CLOSE = "terminal_close"
TERMINAL_READY = "terminal_ready"
TERMINAL_OUTPUT = "terminal_output"
TERMINAL_EXIT = "terminal_exit"
TERMINAL_ERROR = "terminal_error"
_TERMINAL_FRAMES = frozenset({
    TERMINAL_READY, TERMINAL_OUTPUT, TERMINAL_EXIT, TERMINAL_ERROR,
})


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class _StreamCollector:
    """Small client-only accumulator for the stable gateway wire frames.

    Keeping this here avoids importing the server's stream runtime, which in
    turn pulls server command registries and defeats standalone packaging.
    """

    done: asyncio.Event = field(default_factory=asyncio.Event)
    text: str = ""
    model: str | None = None
    attachments: list = field(default_factory=list)
    errored: bool = False
    error_text: str = ""
    delta_text: str = ""
    audio_chunks: list[bytes] = field(default_factory=list)

    def to_legacy_reply(self) -> dict:
        base = {
            "model": self.model,
            "attachments": self.attachments,
            "accumulated": self.delta_text,
            "target": None,
        }
        if self.errored:
            return {**base, "type": "error", "text": self.error_text or self.text or "Error"}
        return {**base, "type": "response", "text": self.text}


def _fold_wire_frame(collector: _StreamCollector, data: dict) -> bool:
    frame_type = data.get("type")
    if frame_type == "delta":
        collector.delta_text += str(data.get("text") or "")
    elif frame_type == "response":
        collector.text = str(data.get("text") or "")
        collector.model = data.get("model") if isinstance(data.get("model"), str) else None
        attachments = data.get("attachments")
        collector.attachments = list(attachments) if isinstance(attachments, list) else []
    elif frame_type == "audio_chunk":
        try:
            collector.audio_chunks.append(base64.b64decode(data.get("data") or ""))
        except Exception:
            pass
    elif frame_type == _P_ERROR:
        collector.errored = True
        collector.error_text = str(data.get("text") or "Error")
        return True
    elif frame_type == "turn_complete":
        return True
    return False


def _session_open_frame(session_id: str, *, profile: str, language: str | None,
                        client_kind: str | None, speak: bool = False) -> dict:
    frame = {
        "type": "session_open",
        "session_id": session_id,
        "ts_ms": _now_ms(),
        "profile": profile,
        "speak": speak,
    }
    if language is not None:
        frame["language"] = language
    if client_kind is not None:
        frame["client_kind"] = client_kind
    return frame


def _text_final_frame(session_id: str, text: str, source: str) -> dict:
    return {
        "type": "text_final",
        "session_id": session_id,
        "ts_ms": _now_ms(),
        "text": text,
        "source": source,
    }


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
        principal_handle: str | None = None,
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
        self.principal_handle = principal_handle

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        # ``_stream_pending``: in-flight collectors keyed by session_id;
        # the listener folds outbound events into them via
        # ``_fold_wire_frame`` and ``send_message`` await ``done``.
        # ``_opened_sessions``: sessions already ``session_open``'d on
        # this WS — cleared on disconnect since the gateway tears down
        # server-side ``StreamSession``s when the WS drops.
        self._stream_pending: dict[str, _StreamCollector] = {}
        self._command_future: asyncio.Future | None = None
        self._opened_sessions: set[str] = set()
        self._status_cb: dict[str, Callable] = {}
        # Per-session sink for the transient ``reasoning`` frame
        # ({"type":"reasoning","active":bool,...}). Keyed by session_id
        # like ``_status_cb``; the callback receives the bool ``active``.
        self._reasoning_cb: dict[str, Callable] = {}
        # Per-session sink for the ``session_compacted`` frame (vision §2
        # in-place compaction). Keyed by session_id like the others; the
        # callback receives the raw frame dict so it can read ``phase`` +
        # the token/run stats and render a step line.
        self._compaction_cb: dict[str, Callable] = {}
        # Single sink for terminal frames (terminal_output / _ready /
        # _exit / _error). The ``terminal`` command installs one while
        # it owns the foreground; ``None`` the rest of the time.
        self._terminal_cb: Callable[[dict], None] | None = None
        self._listener_task: asyncio.Task | None = None
        self.agent_name: str | None = None
        self.agent_version: str | None = None
        self.agent_handle: str | None = None
        self.network_id: str | None = None
        # Created after auth, and therefore scoped to this exact server,
        # network and principal.  It is in-memory only and is dropped on
        # disconnect so capability/search state cannot cross accounts.
        self._remote_api = None

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
        from .network.client.login import list_agents as coord_list_agents
        from .network.client.login import refresh_cert
        from .network.auth.device_cert import verify_cert
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        store = user_store.load()
        net = user_store.find(store, network_name, handle)
        if net is None:
            raise LookupError(
                f"network {network_name!r} not in user store; run `openagent-cli connect "
                f"{handle}@{network_name} --invite <code>` first",
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
                from .network.user_store import save
                import time as _time
                net.last_login_at = _time.time()
                save(store)
            except BaseException:
                # Cancellation before ownership is transferred to a returned
                # GatewayClient must not strand the Iroh node.
                await node.stop()
                raise
        else:
            await node.start()

        from .network.client.session import NetworkBinding
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
        proxy = None
        try:
            agents = await coord_list_agents(
                node=node, coordinator_node_id=net.coordinator_node_id
            )
            if not agents:
                raise LookupError(f"no agents registered in network {network_name!r}")

            chosen = None
            if target_agent_handle:
                chosen = next(
                    (a for a in agents if a.get("handle") == target_agent_handle), None
                )
            elif store.active_agent:
                chosen = next(
                    (a for a in agents if a.get("handle") == store.active_agent), None
                )
            if chosen is None:
                chosen = agents[0]
            target_node_id = chosen["node_id"]
            target_handle = chosen["handle"]

            proxy = LoopbackProxy(dialer=dialer, target_node_id=target_node_id)
            await proxy.start()
        except BaseException:
            # This factory has no caller-owned client yet, so it owns cleanup
            # for every failure/cancellation through proxy startup.
            if proxy is not None:
                try:
                    await proxy.stop()
                except BaseException:
                    pass
            try:
                await dialer.close()
            except BaseException:
                pass
            try:
                await node.stop()
            except BaseException:
                pass
            raise

        return cls(
            proxy=proxy,
            node=node,
            dialer=dialer,
            target_handle=target_handle,
            principal_handle=handle,
        )

    @property
    def base_url(self) -> str:
        return self.url.replace("ws://", "http://").replace("/ws", "")

    @property
    def is_connected(self) -> bool:
        """True while the underlying websocket is open."""
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self.url)
            # Legacy AUTH frame is ignored by the new gateway, but sending
            # it costs nothing and keeps wire compatibility tests passing.
            await self._ws.send_json({"type": _P_AUTH, "client_id": "cli"})
            resp = await self._ws.receive_json()
            if resp.get("type") == _P_AUTH_ERROR:
                # The reason is server-controlled and can contain echoed
                # account/input data; callers only need the stable category.
                raise ConnectionError("Gateway authentication failed")
            self.agent_name = resp.get("agent_name")
            self.agent_version = resp.get("version")
            self.agent_handle = resp.get("handle")
            self.network_id = resp.get("network")
            from .remote_api import RemoteAPIClient
            embedded = resp.get("capabilities")
            self._remote_api = RemoteAPIClient(
                session=self._session,
                base_url=self.base_url,
                cache_scope=(
                    self.base_url,
                    str(self.network_id or ""),
                    str(self.principal_handle or ""),
                ),
                embedded_capabilities=embedded if isinstance(embedded, dict) else None,
            )
            self._listener_task = asyncio.create_task(self._listen())
        except BaseException:
            # Covers auth failure and cancellation while dialing/receiving.
            # Without this, the one-shot REST commands could lose the client
            # before returning it to their ``finally`` and leak a ClientSession.
            try:
                await self.disconnect()
            except BaseException:
                pass
            raise

    @property
    def operational_api(self):
        """History/search client for the authenticated gateway principal."""
        if self._remote_api is None:
            raise RuntimeError("connect() must complete before using the operational API")
        return self._remote_api

    async def get_capabilities(self, *, force: bool = False) -> dict | None:
        """Return account-scoped capabilities, or ``None`` for a legacy server."""
        return await self.operational_api.capabilities(force=force)

    async def list_operational_history(self, query, *, all_pages: bool = False) -> dict:
        """Read unified history without ever opening the server database."""
        return await self.operational_api.collect_history(query, all_pages=all_pages)

    async def search_operational_history(self, query, *, all_pages: bool = False) -> dict:
        """Search authorized operational data through the POST-body API."""
        return await self.operational_api.collect_search(query, all_pages=all_pages)

    async def disconnect(self) -> None:
        listener = self._listener_task
        self._listener_task = None
        if listener and listener is not asyncio.current_task():
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        ws = self._ws
        self._ws = None
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        session = self._session
        self._session = None
        if session:
            try:
                await session.close()
            except Exception:
                pass
        if self._proxy is not None:
            try:
                await self._proxy.stop()
            except Exception:
                pass
            self._proxy = None
        if self._dialer is not None:
            try:
                await self._dialer.close()
            except Exception:
                pass
            self._dialer = None
        if self._node is not None:
            try:
                await self._node.stop()
            except Exception:
                pass
            self._node = None
        self._opened_sessions.clear()
        self._stream_pending.clear()
        self._remote_api = None

    async def _listen(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                t = data.get("type")

                # Terminal frames are routed to the active terminal sink and
                # never touch the chat stream collectors.
                if t in _TERMINAL_FRAMES:
                    cb = self._terminal_cb
                    if cb is not None:
                        try:
                            cb(data)
                        except Exception:  # noqa: BLE001
                            logger.debug("terminal handler error", exc_info=True)
                    continue

                sid = data.get("session_id")
                collector = self._stream_pending.get(sid) if sid else None

                if t == _P_STATUS:
                    cb = self._status_cb.get(sid)
                    if cb is not None:
                        # Guarded like the terminal/reasoning/turn-final sinks
                        # below: a raising on_status must not kill the listener
                        # and strand the in-flight turn.
                        try:
                            await cb(data.get("text", ""))
                        except Exception:  # noqa: BLE001
                            logger.debug("status handler error", exc_info=True)
                    continue
                if t == "reasoning":
                    rcb = self._reasoning_cb.get(sid)
                    if rcb is not None:
                        try:
                            await rcb(bool(data.get("active", False)))
                        except Exception:  # noqa: BLE001
                            logger.debug("reasoning handler error", exc_info=True)
                    continue
                if t == _P_SESSION_COMPACTED:
                    ccb = self._compaction_cb.get(sid)
                    if ccb is not None:
                        try:
                            await ccb(data)
                        except Exception:  # noqa: BLE001
                            logger.debug("compaction handler error", exc_info=True)
                    continue
                if t == _P_COMMAND_RESULT:
                    if self._command_future is not None and not self._command_future.done():
                        self._command_future.set_result(data)
                        self._command_future = None
                    continue
                if t == _P_ERROR and collector is None:
                    # Do not put the server's free-form error text in client
                    # logs: it may contain user/tool input.
                    logger.warning("gateway error without a matching session")
                    continue

                if collector is None:
                    continue
                if _fold_wire_frame(collector, data):
                    rcb = self._reasoning_cb.get(sid)
                    if rcb is not None:
                        try:
                            await rcb(False)
                        except Exception:  # noqa: BLE001
                            logger.debug("reasoning handler error", exc_info=True)
                    collector.done.set()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Parsing/transport failures must release waiters, but logging the
            # frame/exception could echo private conversation text.
            logger.warning("gateway listener stopped after a protocol error")
        finally:
            if self._listener_task is asyncio.current_task():
                self._listener_task = None
            for collector in tuple(self._stream_pending.values()):
                if not collector.done.is_set():
                    collector.errored = True
                    collector.error_text = "Gateway connection closed"
                    collector.done.set()
            command_future = self._command_future
            self._command_future = None
            if command_future is not None and not command_future.done():
                command_future.set_exception(ConnectionError("gateway connection closed"))

    async def send_message(
        self,
        text: str,
        session_id: str,
        on_status: Callable | None = None,
        *,
        source: str = "user_typed",
        on_reasoning: Callable | None = None,
        on_compaction: Callable | None = None,
    ) -> dict:
        """Push a typed message into the user's stream session and await the reply.

        ``on_reasoning(active: bool)`` — if supplied — is awaited whenever a
        ``reasoning`` frame arrives for this session (active=true when the
        agent is thinking with no visible output yet, false once output
        starts or the turn ends). It is also driven to ``False`` on the
        turn-final frame as a safety net, then cleared on return.
        """
        if session_id not in self._opened_sessions:
            await self._ws.send_json(_session_open_frame(
                session_id,
                profile="batched",
                language=None,
                client_kind="cli",
                speak=False,
            ))
            self._opened_sessions.add(session_id)

        collector = _StreamCollector()
        self._stream_pending[session_id] = collector
        if on_status:
            self._status_cb[session_id] = on_status
        if on_reasoning:
            self._reasoning_cb[session_id] = on_reasoning
        if on_compaction:
            self._compaction_cb[session_id] = on_compaction

        try:
            await self._ws.send_json(_text_final_frame(session_id, text, source))
        except Exception:
            self._stream_pending.pop(session_id, None)
            self._status_cb.pop(session_id, None)
            self._reasoning_cb.pop(session_id, None)
            self._compaction_cb.pop(session_id, None)
            raise

        try:
            await collector.done.wait()
        finally:
            self._stream_pending.pop(session_id, None)
            self._status_cb.pop(session_id, None)
            self._reasoning_cb.pop(session_id, None)
            self._compaction_cb.pop(session_id, None)

        return collector.to_legacy_reply()

    async def send_command(
        self, name: str, arg: str | None = None, session_id: str | None = None,
    ) -> str:
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._command_future = fut
        try:
            payload: dict = {"type": _P_COMMAND, "name": name}
            if arg is not None:
                payload["arg"] = arg
            if session_id is not None:
                payload["session_id"] = session_id
            await self._ws.send_json(payload)
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
        await self._ws.send_json(_session_open_frame(
            session_id,
            profile=profile,
            language=language,
            client_kind=client_kind,
        ))

    async def send_session_close(self, session_id: str) -> None:
        await self._ws.send_json({
            "type": "session_close",
            "session_id": session_id,
            "ts_ms": _now_ms(),
        })

    async def send_text_final(
        self, session_id: str, text: str, *, source: str = "user_typed"
    ) -> None:
        await self._ws.send_json(_text_final_frame(session_id, text, source))

    async def send_audio_chunk_in(
        self,
        session_id: str,
        data: bytes,
        *,
        end_of_speech: bool = False,
        sample_rate: int | None = None,
        encoding: str | None = None,
    ) -> None:
        await self._ws.send_json({
            "type": "audio_chunk_in",
            "session_id": session_id,
            "ts_ms": _now_ms(),
            "data": base64.b64encode(data).decode("ascii"),
            "end_of_speech": bool(end_of_speech),
            "sample_rate": sample_rate or 0,
            "encoding": encoding or "",
        })

    async def send_interrupt(
        self, session_id: str, *, reason: str = "manual"
    ) -> None:
        await self._ws.send_json({
            "type": "interrupt",
            "session_id": session_id,
            "ts_ms": _now_ms(),
            "reason": reason,
        })

    # ── Interactive terminal helpers ────────────────────────────────

    def set_terminal_handler(self, cb: Callable[[dict], None] | None) -> None:
        """Install (or clear) the sink for inbound terminal frames."""
        self._terminal_cb = cb

    async def send_terminal_open(
        self,
        terminal_id: str,
        *,
        cols: int,
        rows: int,
        cwd: str | None = None,
        shell: str | None = None,
    ) -> None:
        payload = {
            "type": TERMINAL_OPEN,
            "terminal_id": terminal_id,
            "cols": int(cols),
            "rows": int(rows),
        }
        if cwd:
            payload["cwd"] = cwd
        if shell:
            payload["shell"] = shell
        await self._ws.send_json(payload)

    async def send_terminal_input(self, terminal_id: str, data: bytes) -> None:
        import base64
        await self._ws.send_json({
            "type": TERMINAL_INPUT,
            "terminal_id": terminal_id,
            "data": base64.b64encode(data).decode("ascii"),
        })

    async def send_terminal_resize(
        self, terminal_id: str, cols: int, rows: int
    ) -> None:
        await self._ws.send_json({
            "type": TERMINAL_RESIZE,
            "terminal_id": terminal_id,
            "cols": int(cols),
            "rows": int(rows),
        })

    async def send_terminal_signal(self, terminal_id: str, signal_name: str) -> None:
        await self._ws.send_json({
            "type": TERMINAL_SIGNAL,
            "terminal_id": terminal_id,
            "signal": signal_name,
        })

    async def send_terminal_close(self, terminal_id: str) -> None:
        await self._ws.send_json({
            "type": TERMINAL_CLOSE,
            "terminal_id": terminal_id,
        })

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
