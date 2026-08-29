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

from pathlib import Path
import json
import os
import platform
import sys

from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

# Build-environment guard — see openagent.spec for rationale.
import iroh  # noqa: F401 — P2P transport, Rust FFI dylib must be bundled

block_cipher = None

# ── Hidden imports ──
# The CLI is much lighter than the server: just click, rich, aiohttp, and
# the openagent_cli package itself.

hiddenimports = [
    *collect_submodules("openagent_cli"),
    *collect_submodules("openagent_client_transport"),
    *collect_submodules("rich"),
    *collect_submodules("aiohttp"),
    *collect_submodules("openagent_host_tools"),
    *collect_submodules("anyio"),
    "click",
    # iroh: see openagent.spec for the full explanation. The CLI uses
    # iroh via openagent.network.iroh_node + .client.session for the
    # ``loopback`` / ``connect`` flows.
    "iroh",
    "iroh.iroh_ffi",
    *collect_submodules("iroh"),
    "cbor2",
    "srptools",
    "cryptography",
    *collect_submodules("cryptography"),
]

# ── Data files ──
# certifi CA bundle for HTTPS requests (aiohttp needs this when bundled)

datas = []
datas += collect_data_files("certifi")
datas += collect_data_files("openagent_host_tools")

# Release builds require the exact native host bundle because the packaging
# step installs it beside the CLI. Do not put it inside PyInstaller's one-file
# archive: PyInstaller re-signs nested Mach-O binaries ad hoc, invalidating the
# bundle checksum and computer-control's stable macOS TCC identity.
_arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
_platform = {"darwin": "darwin", "linux": "linux", "win32": "win32"}.get(sys.platform)
if _platform:
    _configured_bundle = os.environ.get("OPENAGENT_HOST_TOOLS_BUNDLE")
    _release_build = os.environ.get("OPENAGENT_RELEASE_BUILD") == "1"
    if _release_build and not _configured_bundle:
        raise SystemExit(
            "OPENAGENT_HOST_TOOLS_BUNDLE is required for a release CLI build"
        )
    _bundle = (
        Path(_configured_bundle).resolve()
        if _configured_bundle
        else Path("..").resolve() / "openagent-host-tools" / "dist" / f"{_platform}-{_arch}"
    )
    if _configured_bundle and not _bundle.is_dir():
        raise SystemExit(f"configured host-tools bundle does not exist: {_bundle}")
    if _bundle.is_dir():
        _manifest_path = _bundle / "bundle-manifest.json"
        if not _manifest_path.is_file():
            raise SystemExit(f"host-tools bundle manifest is missing: {_manifest_path}")
        _manifest = json.loads(_manifest_path.read_text(encoding="utf-8"))
        if (
            _manifest.get("manifest_version") != 1
            or _manifest.get("platform") != f"{_platform}-{_arch}"
            or not isinstance(_manifest.get("files"), dict)
            or not _manifest["files"]
        ):
            raise SystemExit(f"host-tools bundle identity is invalid: {_manifest_path}")
    elif _release_build:
        raise SystemExit(f"release host-tools bundle is missing: {_bundle}")

# ── Dynamic libs ──
# iroh's Rust FFI library (libiroh_ffi.{so,dylib,dll}) — see openagent.spec.
binaries = collect_dynamic_libs("iroh")

# ── Analysis ──

a = Analysis(
    ["src/openagent_cli/main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
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
        # Exclude the full openagent server — CLI is a thin client
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
