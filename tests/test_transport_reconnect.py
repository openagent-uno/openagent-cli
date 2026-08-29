from __future__ import annotations

import pytest

from openagent_client_transport.network.client.session import (
    NetworkBinding,
    SessionDialer,
)


TARGET = "a" * 64
CERT = b"\x11\x22\x33"


class _Send:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    async def write_all(self, value: bytes) -> None:
        self.writes.append(bytes(value))


class _Bi:
    def __init__(self) -> None:
        self._send = _Send()
        self._recv = object()

    def send(self):
        return self._send

    def recv(self):
        return self._recv


class _Connection:
    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.open_count = 0
        self.closed = False
        self.streams: list[_Bi] = []

    async def open_bi(self):
        self.open_count += 1
        if self.failure is not None:
            raise RuntimeError(self.failure)
        stream = _Bi()
        self.streams.append(stream)
        return stream

    def close(self, _code, _reason) -> None:
        self.closed = True


class _Node:
    def __init__(self, connections: list[_Connection]) -> None:
        self.connections = list(connections)
        self.dials: list[tuple[str, bytes]] = []

    async def dial(self, node_id: str, alpn: bytes):
        self.dials.append((node_id, alpn))
        if not self.connections:
            raise AssertionError("unexpected extra redial")
        return self.connections.pop(0)


def _dialer(node: _Node) -> SessionDialer:
    return SessionDialer(
        node=node,
        binding=NetworkBinding(
            network_id="network-1",
            network_name="test",
            coordinator_node_id="b" * 64,
            coordinator_pubkey_bytes=b"c" * 32,
            our_handle="alice",
        ),
        cert_wire=CERT,
    )


@pytest.mark.asyncio
async def test_dropped_cached_connection_is_evicted_and_redialled_once():
    dropped = _Connection(failure="connection lost")
    replacement = _Connection()
    node = _Node([dropped, replacement])
    dialer = _dialer(node)

    stream = await dialer.open_gateway_stream(TARGET)

    assert stream.target_node_id == TARGET
    assert len(node.dials) == 2
    assert dropped.open_count == 1
    assert dropped.closed is True
    assert replacement.open_count == 1
    assert replacement.streams[0]._send.writes == [
        len(CERT).to_bytes(4, "big") + CERT
    ]

    await dialer.open_gateway_stream(TARGET)
    assert len(node.dials) == 2, "the healthy replacement should remain pooled"
    assert replacement.open_count == 2
    await dialer.close()


@pytest.mark.asyncio
async def test_failed_replacement_is_evicted_without_unbounded_redial():
    first = _Connection(failure="first connection lost")
    second = _Connection(failure="replacement also lost")
    node = _Node([first, second])
    dialer = _dialer(node)

    with pytest.raises(RuntimeError, match="replacement also lost"):
        await dialer.open_gateway_stream(TARGET)

    assert len(node.dials) == 2, "only the initial dial and one redial are allowed"
    assert first.closed is True
    assert second.closed is True
