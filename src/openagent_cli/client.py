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
import uuid
from typing import Callable

import aiohttp

from openagent_client_transport.network import user_store
from openagent_client_transport.network.client.session import LoopbackProxy, SessionDialer
from openagent_client_transport.network.identity import (
    Identity,
    load_or_create_identity,
)
from openagent_client_transport.network.iroh_node import IrohNode
from openagent_client_transport.stream.collector import StreamCollector, fold_outbound_event
from openagent_client_transport.stream.events import (
    AudioChunk, Interrupt, SessionClose, SessionOpen, TextFinal, now_ms,
)
from openagent_client_transport.stream.wire import event_to_wire, wire_to_event

logger = logging.getLogger(__name__)

# Gateway + terminal frame types are stable public wire strings. Keeping this
# small set local prevents a thin CLI build from importing server commands.
_P_AUTH = "auth"
_P_AUTH_ERROR = "auth_error"
_P_COMMAND = "command"
_P_COMMAND_RESULT = "command_result"
_P_STATUS = "status"
_P_ERROR = "error"
_P_SESSION_COMPACTED = "session_compacted"

_CAPABILITY_HELLO_ACK = "capability_hello_ack"
_CAPABILITY_PROTOCOL = "client-capabilities/1"

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
        # One opaque instance id binds this interactive chat socket to the
        # separate /ws/capabilities socket. It is never a device selector or a
        # fallback: the gateway verifies both sockets from the same device cert.
        self.client_instance_id = uuid.uuid4().hex
        self._capability_host = None
        self._capability_bridge = None
        self._capability_principal: dict | None = None
        self._capability_ws: aiohttp.ClientWebSocketResponse | None = None
        self._capability_listener_task: asyncio.Task | None = None
        self._capability_heartbeat_task: asyncio.Task | None = None
        self._capability_supervisor_task: asyncio.Task | None = None
        self._capability_transport_close_task: asyncio.Task | None = None
        # Stable for this process. A transport reconnect is the same execution
        # host: retaining the generation lets the Gateway transfer background
        # shell correlation instead of treating the process as a replacement.
        self._capability_generation = 1
        self._capabilities_stopping = False
        self.agent_name: str | None = None
        self.agent_version: str | None = None
        self.agent_handle: str | None = None
        self.network_name: str | None = None
        self.network_id: str | None = None
        self._certified_device_id: str | None = None
        if dialer is not None:
            try:
                cert = dialer.parsed_cert()
            except Exception:  # noqa: BLE001 - disables capabilities fail-closed
                logger.warning("could not derive capability identity from device cert")
            else:
                self.network_id = cert.network_id
                self._certified_device_id = cert.device_pubkey_hex

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
        from openagent_client_transport.network.client.login import list_agents as coord_list_agents
        from openagent_client_transport.network.client.login import refresh_cert
        from openagent_client_transport.network.auth.device_cert import verify_cert
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
                from openagent_client_transport.network.user_store import save
                import time as _time
                net.last_login_at = _time.time()
                save(store)
            except Exception:
                await node.stop()
                raise
        else:
            await node.start()

        from openagent_client_transport.network.client.session import NetworkBinding
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

    @property
    def is_connected(self) -> bool:
        """True while the underlying websocket is open."""
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.url)
        # Legacy AUTH frame is ignored by the new gateway, but sending
        # it costs nothing and keeps wire compatibility tests passing.
        await self._ws.send_json({"type": _P_AUTH, "client_id": "cli"})
        resp = await self._ws.receive_json()
        if resp.get("type") == _P_AUTH_ERROR:
            raise ConnectionError(f"Auth failed: {resp.get('reason')}")
        self.agent_name = resp.get("agent_name")
        self.agent_version = resp.get("version")
        self.agent_handle = resp.get("handle")
        self.network_name = resp.get("network")
        reported_network_id = resp.get("network_id")
        if self.network_id is not None:
            if reported_network_id != self.network_id:
                raise ConnectionError(
                    "gateway authenticated a different network than the device certificate"
                )
        else:
            # Legacy/raw debugging clients may still retain the old field for
            # display. They do not get local capabilities because there is no
            # certified device id to bind the dedicated socket to.
            self.network_id = reported_network_id or resp.get("network")
        self._listener_task = asyncio.create_task(self._listen())
        await self._start_local_capabilities()

    async def disconnect(self) -> None:
        await self._stop_local_capabilities()
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

    async def _start_local_capabilities(self) -> None:
        """Offer this interactive CLI computer over the dedicated capability WS.

        Missing host-tools, disabled consent, and older gateways are all clean
        degradations: chat remains connected and server tools remain unchanged.
        """
        try:
            from openagent_host_tools import CapabilityBridge, LocalCapabilityClient
        except ImportError:
            logger.info("openagent-host-tools is not installed; local tools unavailable")
            return

        if not self.network_id or not self._certified_device_id:
            logger.info("gateway supplied no certified account/network id; local tools disabled")
            return
        host = LocalCapabilityClient()
        try:
            await host.start()
            self._capability_host = host
            self._capabilities_stopping = False
            await self._connect_capabilities_once()
            self._capability_supervisor_task = asyncio.create_task(
                self._supervise_capabilities(), name="cli-capability-supervisor"
            )
        except Exception as exc:  # noqa: BLE001 - compatibility degradation
            logger.info("local capability connection unavailable: %s", exc)
            try:
                await host.close()
            except Exception:
                pass
            if self._capability_ws is not None:
                try:
                    await self._capability_ws.close()
                except Exception:
                    pass
            self._capability_host = None
            self._capability_bridge = None
            self._capability_ws = None

    async def _connect_capabilities_once(self) -> bool:
        host = self._capability_host
        if host is None or self._session is None or not self.network_id:
            return False
        status = await host.status()
        if not bool((status.get("consent") or {}).get("enabled")):
            return False
        base = self.url[:-3] if self.url.endswith("/ws") else self.url.rsplit("/", 1)[0]
        capability_url = f"{base}/ws/capabilities"
        ws = await self._session.ws_connect(capability_url)
        generation = self._capability_generation
        from openagent_host_tools import CapabilityBridge

        bridge = CapabilityBridge(
            host,
            client_instance_id=self.client_instance_id,
            send_json=ws.send_json,
            generation=generation,
            trusted_account_id=self.network_id,
            trusted_device_id=self._certified_device_id,
            on_transport_lost=lambda: self._schedule_capability_transport_close(
                ws, bridge
            ),
        )
        self._capability_principal = dict(bridge.principal)
        try:
            await bridge.hello()
            ack_msg = await ws.receive(timeout=10)
            if ack_msg.type != aiohttp.WSMsgType.TEXT:
                raise ConnectionError("capability gateway closed before hello acknowledgement")
            ack = json.loads(ack_msg.data)
            if ack.get("type") != _CAPABILITY_HELLO_ACK or ack.get("accepted") is not True:
                raise ConnectionError(f"capability hello rejected: {ack}")
            if ack.get("protocol") != _CAPABILITY_PROTOCOL:
                raise ConnectionError("capability hello acknowledged a different protocol")
            if ack.get("device_id") != self._certified_device_id:
                raise ConnectionError("capability hello acknowledged a different device")
            if ack.get("account_id") != self.network_id:
                raise ConnectionError("capability hello acknowledged a different account")
            if ack.get("client_instance_id") != self.client_instance_id:
                raise ConnectionError("capability hello acknowledged a different client instance")
            if ack.get("generation") is None or int(ack["generation"]) != generation:
                raise ConnectionError("capability hello acknowledged a stale generation")
            self._capability_bridge = bridge
            self._capability_ws = ws
            bridge.activate_events()
        except Exception:
            if self._capability_bridge is bridge:
                self._capability_bridge = None
            if self._capability_ws is ws:
                self._capability_ws = None
            await bridge.close()
            await ws.close()
            raise
        self._capability_listener_task = asyncio.create_task(
            self._listen_capabilities(), name="cli-capability-listener"
        )
        self._capability_heartbeat_task = asyncio.create_task(
            self._heartbeat_capabilities(), name="cli-capability-heartbeat"
        )
        return True

    def _schedule_capability_transport_close(self, ws, bridge) -> None:
        """Drop only the exact capability WS after local-host transport loss.

        This callback is deliberately synchronous: it is invoked from the
        broker listener, so awaiting WebSocket/bridge teardown there would
        create a listener-close cycle. The supervisor observes the WS close,
        tears down this bridge without releasing its stable principal, and
        reconnects with the same instance and generation.
        """

        if self._capabilities_stopping:
            return
        active = self._capability_transport_close_task
        if active is not None and not active.done():
            return

        async def close_exact_socket() -> None:
            if self._capability_ws is not ws or self._capability_bridge is not bridge:
                return
            try:
                await ws.close(code=1011, message=b"local capability host transport lost")
            except TypeError:
                # Lightweight/fake WebSocket implementations may only expose
                # close() without aiohttp's optional code/message parameters.
                await ws.close()
            except Exception:
                logger.debug("failed to close lost capability transport", exc_info=True)

        task = asyncio.create_task(
            close_exact_socket(), name="cli-capability-host-transport-lost"
        )
        self._capability_transport_close_task = task

        def clear(completed: asyncio.Task) -> None:
            if self._capability_transport_close_task is completed:
                self._capability_transport_close_task = None

        task.add_done_callback(clear)

    async def _supervise_capabilities(self) -> None:
        backoff = 0.25
        try:
            while not self._capabilities_stopping:
                active = [
                    task
                    for task in (
                        self._capability_listener_task,
                        self._capability_heartbeat_task,
                    )
                    if task is not None
                ]
                if active:
                    await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    await self._close_capability_connection(release_principal=False)
                if self._capabilities_stopping or not self.is_connected:
                    return
                await asyncio.sleep(backoff)
                try:
                    connected = await self._connect_capabilities_once()
                except Exception:
                    connected = False
                    logger.debug("capability reconnect failed", exc_info=True)
                backoff = 0.25 if connected else min(backoff * 2, 5.0)
        except asyncio.CancelledError:
            raise

    async def _listen_capabilities(self) -> None:
        ws = self._capability_ws
        bridge = self._capability_bridge
        if ws is None or bridge is None:
            return
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await bridge.handle(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("capability listener ended", exc_info=True)

    async def _heartbeat_capabilities(self) -> None:
        bridge = self._capability_bridge
        host = self._capability_host
        if bridge is None or host is None:
            return
        last_enabled = True
        try:
            while True:
                await asyncio.sleep(15)
                status = await host.status()
                enabled = bool((status.get("consent") or {}).get("enabled"))
                if enabled != last_enabled:
                    await bridge.catalog_update()
                    last_enabled = enabled
                await bridge.heartbeat()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("capability heartbeat ended", exc_info=True)

    async def _stop_local_capabilities(self) -> None:
        self._capabilities_stopping = True
        if self._capability_supervisor_task is not None:
            self._capability_supervisor_task.cancel()
            await asyncio.gather(self._capability_supervisor_task, return_exceptions=True)
            self._capability_supervisor_task = None
        await self._close_capability_connection(release_principal=False)
        transport_close = self._capability_transport_close_task
        if transport_close is not None and transport_close is not asyncio.current_task():
            transport_close.cancel()
            await asyncio.gather(transport_close, return_exceptions=True)
        self._capability_transport_close_task = None
        if self._capability_host is not None:
            try:
                if self._capability_principal is not None:
                    await self._capability_host.release_principal(
                        self._capability_principal,
                    )
            except Exception:
                pass
            try:
                await self._capability_host.close()
            except Exception:
                pass
        self._capability_host = None
        self._capability_principal = None

    async def _close_capability_connection(self, *, release_principal: bool = False) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in (
                self._capability_heartbeat_task,
                self._capability_listener_task,
            )
            if task is not None and task is not current
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._capability_heartbeat_task = None
        self._capability_listener_task = None
        if self._capability_bridge is not None:
            try:
                await self._capability_bridge.close(
                    release_principals=release_principal,
                )
            except Exception:
                pass
        if self._capability_ws is not None:
            try:
                await self._capability_ws.close()
            except Exception:
                pass
        self._capability_bridge = None
        self._capability_ws = None

    async def _listen(self) -> None:
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
                    # below: a raising on_status (e.g. a tool-error string
                    # with Rich markup) must NOT kill the listener — that
                    # would strand ``collector.done`` and hang the turn.
                    try:
                        await cb(data.get("text", ""))
                    except Exception:  # noqa: BLE001
                        logger.debug("status handler error", exc_info=True)
                continue
            if t == "reasoning":
                # Transient, session-scoped "is the agent thinking with
                # no visible output yet?" signal. Route ``active`` to the
                # per-session reasoning sink; a missing sink is a no-op.
                rcb = self._reasoning_cb.get(sid)
                if rcb is not None:
                    try:
                        await rcb(bool(data.get("active", False)))
                    except Exception:  # noqa: BLE001
                        logger.debug("reasoning handler error", exc_info=True)
                continue
            if t == _P_SESSION_COMPACTED:
                # In-place compaction progress (vision §2): running →
                # done/error. Route the raw frame to the per-session sink
                # so the turn renderer can print a "Compacting…" step line;
                # a missing sink (e.g. a bare /compact command with no live
                # turn) is a harmless no-op.
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
                logger.warning("gateway error (no session): %s", data.get("text"))
                continue

            evt = wire_to_event(data)
            if evt is None or collector is None:
                continue
            if fold_outbound_event(collector, evt):
                # Safety net: clear any lingering reasoning state on the
                # turn-final frame in case an explicit active=false was
                # never sent (or was missed).
                rcb = self._reasoning_cb.get(sid)
                if rcb is not None:
                    try:
                        await rcb(False)
                    except Exception:  # noqa: BLE001
                        logger.debug("reasoning handler error", exc_info=True)
                collector.done.set()

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
            open_frame = event_to_wire(SessionOpen(
                session_id=session_id,
                ts_ms=now_ms(),
                profile="batched",
                client_kind="cli",
                speak=False,
            ))
            open_frame["client_instance_id"] = self.client_instance_id
            await self._ws.send_json(open_frame)
            self._opened_sessions.add(session_id)

        collector = StreamCollector()
        self._stream_pending[session_id] = collector
        if on_status:
            self._status_cb[session_id] = on_status
        if on_reasoning:
            self._reasoning_cb[session_id] = on_reasoning
        if on_compaction:
            self._compaction_cb[session_id] = on_compaction

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
        open_frame = event_to_wire(SessionOpen(
            session_id=session_id, ts_ms=now_ms(),
            profile=profile,  # type: ignore[arg-type]
            language=language,
            client_kind=client_kind,
        ))
        open_frame["client_instance_id"] = self.client_instance_id
        await self._ws.send_json(open_frame)

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
