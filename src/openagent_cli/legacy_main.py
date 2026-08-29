"""Compatibility alias for the canonical :mod:`openagent_cli.main` module.

Local-tools callers historically imported ``legacy_main`` while the beta
history/search work made ``main`` the installed CLI implementation again.
Expose the exact same module object so monkeypatching either import path also
changes the globals used by Click command callbacks.
"""

from __future__ import annotations

import sys

from . import main as _main

sys.modules[__name__] = _main
