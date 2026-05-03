"""WebSocket client for the OpenAgent Gateway."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import aiohttp

from openagent.gateway import protocol as P
from openagent.stream.collector import StreamCollector, fold_outbound_event
from openagent.stream.events import (
    AudioChunk, Interrupt, SessionClose, SessionOpen, TextFinal, now_ms,
)
from openagent.stream.wire import event_to_wire, wire_to_event

logger = logging.getLogger(__name__)


class GatewayClient:
    """Async WebSocket client to an OpenAgent Gateway."""

    def __init__(self, url: str, token: str | None = None):
        self.url = url
        self.token = token
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

    @property
    def base_url(self) -> str:
        return self.url.replace("ws://", "http://").replace("/ws", "")

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.url)
        await self._ws.send_json({"type": P.AUTH, "token": self.token or "", "client_id": "cli"})
        resp = await self._ws.receive_json()
        if resp.get("type") == P.AUTH_ERROR:
            raise ConnectionError(f"Auth failed: {resp.get('reason')}")
        self.agent_name = resp.get("agent_name")
        self.agent_version = resp.get("version")
        self._listener_task = asyncio.create_task(self._listen())

    async def disconnect(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
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
        """Push a typed message into the user's stream session and await the reply.

        Lazily opens a ``batched``-profile session on first call (with
        ``speak=False`` since the CLI is text-only). Returns the legacy
        answer-response dict shape: ``{type, text, model, attachments}``
        or ``{type: "error", text}``.
        """
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
    # Used by scripted CLIs and integration tests that want the typed
    # event vocabulary directly instead of the answer-response wrapper.

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
        # `data` may be any JSON-serializable value (scalar, list, dict)
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

        Used to materialise attachments the agent returned in a
        ``response`` message when this CLI is connected to a remote
        gateway and can't read the path directly. Writes to
        ``dest_path`` and returns the number of bytes written.

        Raises ``RuntimeError`` with the status/reason when the gateway
        rejects the request (401 unauthorised, 404 not found, etc.)
        so the caller can surface a clean error to the user.
        """
        params = {"path": remote_path}
        if self.token:
            params["token"] = self.token
        async with self._session.get(f"{self.base_url}/api/files", params=params) as r:
            if r.status != 200:
                body = await r.text()
                raise RuntimeError(f"{r.status} {body[:200]}")
            total = 0
            with open(dest_path, "wb") as f:
                async for chunk in r.content.iter_chunked(64 * 1024):
                    f.write(chunk)
                    total += len(chunk)
            return total
