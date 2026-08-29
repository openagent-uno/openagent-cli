#!/usr/bin/env python3
"""Vendor the minimal, client-only OpenAgent transport under a safe namespace.

The server historically publishes its Python package as ``src``.  The CLI also
used ``src``, which made its wheel depend on a sibling server checkout and made
the two distributions overwrite one another.  This deterministic generator
copies only the pure client transport closure and rewrites its internal imports
to ``openagent_client_transport``.  Generated sources are committed, so both a
source wheel and the frozen CLI build without network access or sibling repos.

Run from the CLI repository:

    python scripts/vendor-client-transport.py ../openagent-server

``transport-source.json`` records the source commit plus SHA-256 for every
source and generated file.  No server agent/runtime modules are included.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


CLI_ROOT = Path(__file__).resolve().parents[1]
DEST_ROOT = CLI_ROOT / "src" / "openagent_client_transport"

SOURCE_FILES = {
    "src/network/identity.py": "network/identity.py",
    "src/network/iroh_node.py": "network/iroh_node.py",
    "src/network/ticket.py": "network/ticket.py",
    "src/network/user_store.py": "network/user_store.py",
    "src/network/auth/device_cert.py": "network/auth/device_cert.py",
    "src/network/coordinator/pake.py": "network/coordinator/pake.py",
    "src/network/client/login.py": "network/client/login.py",
    "src/network/client/session.py": "network/client/session.py",
    "src/stream/events.py": "stream/events.py",
    "src/stream/wire.py": "stream/wire.py",
    "src/stream/collector.py": "stream/collector.py",
}

REPLACEMENTS = (
    ("from src.network", "from openagent_client_transport.network"),
    ("from src.stream", "from openagent_client_transport.stream"),
    ("from src.gateway import protocol as P", "from openagent_client_transport.stream import protocol as P"),
    (
        "from src.memory.artifacts import public_attachment_ref",
        "from openagent_client_transport.helpers import public_attachment_ref",
    ),
)

GENERATED_FILES = {
    "__init__.py": '''"""Client-only OpenAgent Iroh, authentication and stream transport."""\n\n__version__ = "1.0.0"\n''',
    "network/__init__.py": '''"""OpenAgent client network primitives."""\n\nfrom .identity import Identity, load_or_create_identity\nfrom .iroh_node import IrohNode, NetworkAlpn\n\n__all__ = ["Identity", "IrohNode", "NetworkAlpn", "load_or_create_identity"]\n''',
    "network/auth/__init__.py": '''"""Device-certificate verification primitives."""\n\nfrom .device_cert import CertVerificationError, DeviceCert, verify_cert\n\n__all__ = ["CertVerificationError", "DeviceCert", "verify_cert"]\n''',
    "network/client/__init__.py": '''"""Coordinator login and authenticated Gateway dialling."""\n\nfrom .session import LoopbackProxy, NetworkBinding, SessionDialer\n\n__all__ = ["LoopbackProxy", "NetworkBinding", "SessionDialer"]\n''',
    "network/coordinator/__init__.py": '''"""Client-side PAKE primitives; no coordinator service is bundled."""\n''',
    "stream/__init__.py": '''"""Typed Gateway stream events and wire codec."""\n''',
    "stream/protocol.py": '''"""Stable Gateway constants needed by the client stream codec."""\n\nMESSAGE = "message"\nSTATUS = "status"\nDELTA = "delta"\nRESPONSE = "response"\nERROR = "error"\nAUDIO_START = "audio_start"\nAUDIO_CHUNK = "audio_chunk"\nAUDIO_END = "audio_end"\n''',
    "stream/reply.py": '''"""Client-side accumulated batched reply, independent of StreamSession."""\n\nfrom __future__ import annotations\n\nfrom dataclasses import dataclass, field\n\n\n@dataclass\nclass BatchedReply:\n    text: str = ""\n    audio_chunks: list[bytes] = field(default_factory=list)\n    audio_format: str | None = None\n    audio_mime: str | None = None\n    voice_id: str | None = None\n    attachments: list[dict] = field(default_factory=list)\n    model: str | None = None\n    errored: bool = False\n    error_text: str | None = None\n\n    @property\n    def audio_bytes(self) -> bytes | None:\n        return b"".join(self.audio_chunks) if self.audio_chunks else None\n''',
    "helpers.py": '''"""Small client helpers extracted from server-only modules."""\n\nfrom __future__ import annotations\n\nfrom typing import Any, Mapping\n\n\ndef parse_handle_at_network(spec: str) -> tuple[str, str]:\n    if "@" not in spec:\n        raise ValueError(f"expected handle@network, got {spec!r}")\n    handle, _, network = spec.partition("@")\n    handle = handle.strip().lower()\n    network = network.strip().lower()\n    if not handle or not network:\n        raise ValueError(f"empty handle or network in {spec!r}")\n    return handle, network\n\n\ndef coordinator_node_id_to_pubkey_bytes(node_id: str) -> bytes:\n    import iroh\n\n    raw = iroh.PublicKey.from_string(node_id).to_bytes()\n    if len(raw) != 32:\n        raise ValueError(f"NodeId pubkey is not 32 bytes: {len(raw)}")\n    return bytes(raw)\n\n\ndef public_attachment_ref(value: Mapping[str, Any]) -> dict[str, Any]:\n    """Return the canonical path-free attachment reference safe for the wire."""\n\n    allowed = (\n        "type", "kind", "filename", "mime_type", "size_bytes", "sha256",\n        "artifact_id", "artifact_link_id", "url", "caption",\n    )\n    return {key: value[key] for key in allowed if value.get(key) is not None}\n''',
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_commit(server_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=server_root, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> None:
    server_root = (
        Path(sys.argv[1]).expanduser().resolve()
        if len(sys.argv) > 1
        else (CLI_ROOT.parent / "openagent-server").resolve()
    )
    missing = [name for name in SOURCE_FILES if not (server_root / name).is_file()]
    if missing:
        raise SystemExit(f"not an OpenAgent server checkout; missing: {missing}")

    source_hashes: dict[str, str] = {}
    generated_hashes: dict[str, str] = {}
    for source_name, destination_name in SOURCE_FILES.items():
        source = server_root / source_name
        raw = source.read_bytes()
        source_hashes[source_name] = _sha256(raw)
        text = raw.decode("utf-8")
        for old, new in REPLACEMENTS:
            text = text.replace(old, new)
        if destination_name == "stream/collector.py":
            text = text.replace(
                "from openagent_client_transport.stream.channel import BatchedReply",
                "from openagent_client_transport.stream.reply import BatchedReply",
            )
        destination = DEST_ROOT / destination_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        rendered = (
            "# Generated by scripts/vendor-client-transport.py; do not edit by hand.\n"
            + text
        )
        destination.write_text(rendered, encoding="utf-8")
        generated_hashes[destination_name] = _sha256(rendered.encode("utf-8"))

    for destination_name, rendered in GENERATED_FILES.items():
        destination = DEST_ROOT / destination_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
        generated_hashes[destination_name] = _sha256(rendered.encode("utf-8"))

    lock = {
        "schema": 1,
        "source_repository": "https://github.com/openagent-uno/openagent-server",
        "source_commit": _source_commit(server_root),
        "source_files": dict(sorted(source_hashes.items())),
        "generated_files": dict(sorted(generated_hashes.items())),
    }
    (DEST_ROOT / "transport-source.json").write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
