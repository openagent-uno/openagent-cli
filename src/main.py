"""Compatibility launcher for older source-checkout commands.

The installable implementation lives in :mod:`openagent_cli.main`. Keep this
module only so ``python src/main.py`` and imports of ``src.main`` do not fork a
second copy of the CLI.
"""

from __future__ import annotations

import importlib
import sys


if __package__:
    from . import openagent_cli as _package

    sys.modules.setdefault("openagent_cli", _package)
    _implementation = importlib.import_module(".openagent_cli.main", __package__)
else:
    _implementation = importlib.import_module("openagent_cli.main")

globals().update({
    name: value
    for name, value in vars(_implementation).items()
    if not name.startswith("__")
})


if __name__ == "__main__":
    _implementation.main()
