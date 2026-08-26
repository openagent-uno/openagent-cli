"""Standalone client slice of the OpenAgent Iroh network protocol.

This package is vendored from the matching server protocol so release wheels
and frozen binaries never depend on an unpublished ``openagent-framework``
Python distribution or a sibling source checkout. Keep changes protocol-only:
server databases, coordinator services, and server command modules do not
belong in the CLI artifact.

Replaces the legacy ``host:port + token`` connection model. Users log
in as ``handle@network`` with a password (PAKE); the coordinator
issues short-lived signed device certificates that gate every inbound
gateway request. See ``docs/network.md`` (or the plan file) for the
architecture overview.

Public re-exports kept intentionally small so callers don't reach into
sub-packages for things that should be stable across the network
layer's refactors.
"""

from openagent_cli.network.identity import Identity, load_or_create_identity
from openagent_cli.network.iroh_node import IrohNode, NetworkAlpn

__all__ = [
    "Identity",
    "IrohNode",
    "NetworkAlpn",
    "load_or_create_identity",
]
