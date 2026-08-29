"""Compatibility import for :mod:`openagent_cli.remote_api`."""

from __future__ import annotations

import importlib
import sys


from . import openagent_cli as _package

sys.modules.setdefault("openagent_cli", _package)
_implementation = importlib.import_module(".openagent_cli.remote_api", __package__)
globals().update({
    name: value
    for name, value in vars(_implementation).items()
    if not name.startswith("__")
})
