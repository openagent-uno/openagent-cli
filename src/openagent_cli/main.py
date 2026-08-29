"""Installed and frozen OpenAgent CLI entrypoint."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _configure_frozen_host_tools() -> None:
    """Locate the checksum-verified native bundle shipped beside the CLI.

    Native helpers deliberately stay outside PyInstaller's one-file archive:
    PyInstaller re-signs nested Mach-O files ad hoc, which would invalidate the
    producer manifest and destroy computer-control's stable TCC identity.
    """

    if not getattr(sys, "frozen", False) or os.environ.get(
        "OPENAGENT_HOST_TOOLS_SIDECAR_DIR"
    ):
        return
    executable_dir = Path(sys.executable).resolve().parent
    candidates = []
    configured = os.environ.get("OPENAGENT_HOST_TOOLS_BUNDLE")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            executable_dir / "host-tools",
            executable_dir.parent / "lib" / "openagent" / "host-tools",
        ]
    )
    for candidate in candidates:
        if (candidate / "bundle-manifest.json").is_file():
            os.environ["OPENAGENT_HOST_TOOLS_SIDECAR_DIR"] = str(candidate.resolve())
            return


def main() -> None:
    _configure_frozen_host_tools()
    from openagent_cli.legacy_main import main as cli_main

    cli_main()


if __name__ == "__main__":
    # PyInstaller executes this file as its script entrypoint; unlike the
    # setuptools console-script wrapper there is nobody else to call main().
    main()
