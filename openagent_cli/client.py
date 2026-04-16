"""WebSocket client for the OpenAgent Gateway."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

import aiohttp

logger = logging.getLogger(__name__)


class GatewayClient:
    """Async WebSocket client to an OpenAgent Gateway."""

    def __init__(self, url: str, token: str | None = None):
        self.url = url
        self.token = token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._pending: dict[str, asyncio.Future] = {}
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
        # Auth
        await self._ws.send_json({"type": "auth", "token": self.token or "", "client_id": "cli"})
        resp = await self._ws.receive_json()
        if resp.get("type") == "auth_error":
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

    async def _listen(self) -> None:
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            t = data.get("type")
            sid = data.get("session_id")
            if t == "status" and sid in self._status_cb:
                await self._status_cb[sid](data.get("text", ""))
            elif t == "response" and sid in self._pending:
                self._pending.pop(sid).set_result(data)
                self._status_cb.pop(sid, None)
            elif t == "error" and sid in self._pending:
                self._pending.pop(sid).set_result(data)
            elif t == "command_result" and "__cmd__" in self._pending:
                self._pending.pop("__cmd__").set_result(data)

    async def send_message(self, text: str, session_id: str, on_status: Callable | None = None) -> dict:
        fut = asyncio.get_event_loop().create_future()
        self._pending[session_id] = fut
        if on_status:
            self._status_cb[session_id] = on_status
        await self._ws.send_json({"type": "message", "text": text, "session_id": session_id})
        return await fut

    async def send_command(self, name: str) -> str:
        fut = asyncio.get_event_loop().create_future()
        self._pending["__cmd__"] = fut
        await self._ws.send_json({"type": "command", "name": name})
        result = await fut
        return result.get("text", "")

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
