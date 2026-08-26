# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for building the OpenAgent CLI standalone executable.

Usage:
    pip install pyinstaller
    pyinstaller cli.spec --clean --noconfirm

Output: dist/openagent-cli (single-file binary).

Onefile mode for the same reason as the server spec: a lighter download
with no ``_internal/`` folder. The CLI bundle is ~13 MB compressed and
starts in well under a second on every subsequent launch.
"""

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

# Build-environment guard — see openagent.spec for rationale.
import iroh  # noqa: F401 — P2P transport, Rust FFI dylib must be bundled

block_cipher = None

# ── Hidden imports ──
# The CLI is much lighter than the server: just click, rich, aiohttp, and
# the openagent_cli package itself.

hiddenimports = [
    *collect_submodules("openagent_cli"),
    *collect_submodules("rich"),
    *collect_submodules("aiohttp"),
    "click",
    # The standalone client protocol uses iroh for the ``loopback`` /
    # ``connect`` flows.  The Rust FFI dylib is collected below.
    "iroh",
    "iroh.iroh_ffi",
    *collect_submodules("iroh"),
    "cbor2",
    "srptools",
    "cryptography",
    *collect_submodules("cryptography"),
]

# ── Dynamic libs ──
# iroh's Rust FFI library (libiroh_ffi.{so,dylib,dll}) — see openagent.spec.
binaries = collect_dynamic_libs("iroh")

# ── Analysis ──

a = Analysis(
    ["scripts/cli_entry.py"],
    pathex=["src"],
    binaries=binaries,
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy packages not needed at runtime
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "PIL",
        "tkinter",
        "test",
        "unittest",
        # Exclude server/LLM packages — the CLI carries only the standalone
        # protocol client under ``openagent_cli.network``.
        "openagent",
        "litellm",
        "mcp",
        "agno",
        "openai",
        "anthropic",
        "google",
        "sqlalchemy",
        "aiosqlite",
        "croniter",
        "yaml",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# onefile mode — see the note in openagent.spec. One artifact per platform.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="openagent-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
