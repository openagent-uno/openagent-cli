"""Small network parsing helpers needed by the standalone CLI.

These functions intentionally avoid importing the server-only coordinator,
database, and Click command modules from ``openagent-framework``.
"""

from __future__ import annotations


def parse_handle_at_network(spec: str) -> tuple[str, str]:
    """Split ``alice@homelab`` and reject incomplete membership names."""
    if "@" not in spec:
        raise ValueError(f"expected handle@network, got {spec!r}")
    handle, _, network = spec.partition("@")
    handle = handle.strip().lower()
    network = network.strip().lower()
    if not handle or not network:
        raise ValueError(f"empty handle or network in {spec!r}")
    return handle, network


def coordinator_node_id_to_pubkey_bytes(node_id: str) -> bytes:
    """Decode an Iroh NodeId into the pinned raw Ed25519 public key."""
    import iroh

    raw = iroh.PublicKey.from_string(node_id).to_bytes()
    if len(raw) != 32:
        raise ValueError(f"NodeId pubkey is not 32 bytes: {len(raw)}")
    return bytes(raw)
