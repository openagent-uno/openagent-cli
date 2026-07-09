"""Console entrypoint for the source-checkout CLI.

The current monorepo keeps the CLI's historical modules under a package
named ``src`` while the framework also exposes its internals through
``src.*``. In an editable checkout those two packages collide. This shim
loads the CLI package first, then extends its package search path with the
sibling server's ``src`` directory so imports like ``src.client`` and
``src.network`` can coexist. It also aliases ``openagent`` to that combined
package for the CLI's compatibility imports.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _prepare_source_checkout() -> None:
    cli_root = Path(__file__).resolve().parents[2]
    server_src = cli_root.parent / "openagent-server" / "src"

    cli_root_s = str(cli_root)
    if cli_root_s not in sys.path:
        sys.path.insert(0, cli_root_s)

    import src  # noqa: PLC0415

    server_src_s = str(server_src)
    if server_src.exists() and server_src_s not in src.__path__:
        src.__path__.append(server_src_s)
    sys.modules.setdefault("openagent", src)


def main() -> None:
    _prepare_source_checkout()

    from src.main import main as legacy_main  # noqa: PLC0415

    legacy_main()
