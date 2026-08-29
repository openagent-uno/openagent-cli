#!/usr/bin/env bash
# Package a freshly-built PyInstaller onefile binary for release.
#
# Usage:
#   scripts/package-release.sh <app> <dist-dir>
#     app        "openagent" or "openagent-cli"
#     dist-dir   where PyInstaller wrote the binary (usually "dist")
#
# Behaviour per platform (detected from ``$RUNNER_OS`` or ``uname -s``):
#
#   macOS     — the .pkg was already built and stapled by
#               sign-notarize-macos.sh. We just shasum it.
#   Linux     — tar the bare binary into ``<app>-<ver>-linux-<arch>.tar.gz``
#               and shasum the archive.
#   Windows   — zip the ``.exe`` into ``<app>-<ver>-windows-<arch>.zip`` and
#               shasum the archive. Works under Git Bash on GHA's
#               windows-latest runner (tar/sha256sum are present; we
#               shell out to PowerShell for Compress-Archive).
#
# Replaces three+three inline ``Package (…)`` / ``Checksum macOS .pkg``
# blocks that used to live in .github/workflows/release.yml, each with
# their own version-extraction duplication.

set -euo pipefail

APP="${1:?usage: $0 <app> <dist-dir>}"
DIST="${2:?usage: $0 <app> <dist-dir>}"

# ── Detect OS and arch in release-filename convention ────────────────

case "${RUNNER_OS:-$(uname -s)}" in
    macOS|Darwin)                 OS="macos" ;;
    Linux)                        OS="linux" ;;
    Windows|MINGW*|CYGWIN*|MSYS*) OS="windows" ;;
    *) echo "Unsupported OS: ${RUNNER_OS:-$(uname -s)}" >&2; exit 1 ;;
esac

# Git Bash itself can be x64-emulated on a native Windows ARM64 runner, so
# ``uname -m`` describes Bash rather than the Python/PyInstaller executable we
# are packaging. The build interpreter is the authoritative binary target on
# every platform.
PYTHON_BIN="${PYTHON:-python}"
ARCH_RAW="$("$PYTHON_BIN" -c 'import platform; print(platform.machine())')"
case "$ARCH_RAW" in
    x86_64|amd64|AMD64)  ARCH="x64" ;;
    aarch64|arm64|ARM64) ARCH="arm64" ;;
    *) ARCH="$ARCH_RAW" ;;
esac

# Resolve the version from installed distribution metadata.  The CLI keeps
# its import packages below ``src/`` but deliberately owns no top-level
# ``src`` package; importing that name can therefore resolve an unrelated
# sibling checkout and silently stamp the wrong version into release assets.
VERSION="$("$PYTHON_BIN" -c "from importlib.metadata import version; print(version('openagent-cli'))")"

# The CLI executes the native capability host out-of-process. Keep the
# producer-built bundle outside PyInstaller's one-file archive so nested
# Mach-O signatures, the manifest hashes, and the stable TCC identity remain
# byte-for-byte intact. The macOS installer receives the same directory in
# sign-notarize-macos.sh; Linux and Windows archives stage it here beside the
# executable.
if [ "$APP" = "openagent-cli" ]; then
    HOST_TOOLS_SOURCE="${OPENAGENT_HOST_TOOLS_BUNDLE:-}"
    if [ -z "$HOST_TOOLS_SOURCE" ] || [ ! -d "$HOST_TOOLS_SOURCE" ]; then
        echo "ERROR: OPENAGENT_HOST_TOOLS_BUNDLE must name the verified native bundle" >&2
        exit 1
    fi
    if [ ! -f "$HOST_TOOLS_SOURCE/bundle-manifest.json" ]; then
        echo "ERROR: host-tools bundle manifest is missing: $HOST_TOOLS_SOURCE" >&2
        exit 1
    fi
    HOST_TOOLS_STAGE="$DIST/host-tools"
    if [ -e "$HOST_TOOLS_STAGE" ]; then
        echo "ERROR: refusing to overwrite staged host-tools directory: $HOST_TOOLS_STAGE" >&2
        exit 1
    fi
    mkdir -p "$HOST_TOOLS_STAGE"
    cp -R "$HOST_TOOLS_SOURCE/." "$HOST_TOOLS_STAGE/"
fi

# Unified SHA-256 helper — macOS has ``shasum``, Linux/Git Bash have
# ``sha256sum``. ``shasum -a 256`` on macOS and ``sha256sum`` on Linux
# both emit the same ``<hash>  <filename>`` line format.
if command -v shasum >/dev/null 2>&1; then
    sha256() { shasum -a 256 "$@"; }
else
    sha256() { sha256sum "$@"; }
fi

# ── Package per platform ──────────────────────────────────────────────

cd "$DIST"

case "$OS" in
    macos)
        # The .pkg was produced by sign-notarize-macos.sh earlier in the
        # job. Shasum whichever .pkg files actually landed — globbing
        # catches both server and CLI without knowing which job we're in.
        PKG="${APP}-${VERSION}-${OS}-${ARCH}.pkg"
        if [ ! -f "$PKG" ]; then
            echo "ERROR: expected $PWD/$PKG but it wasn't produced by the sign step" >&2
            ls -la
            exit 1
        fi
        sha256 "$PKG" > "${PKG}.sha256"
        echo "✓ $PKG (+.sha256)"
        ;;
    linux)
        BIN="$APP"
        [ -f "$BIN" ] || { echo "ERROR: $PWD/$BIN missing (PyInstaller output)" >&2; ls -la; exit 1; }
        chmod +x "$BIN"
        # Bundle native helpers beside their frozen executable. They remain
        # external to PyInstaller so their producer manifest stays valid.
        FILES=("$BIN")
        if [ "$APP" = "openagent" ] && [ -f "openagent-computer-control" ]; then
            chmod +x "openagent-computer-control"
            FILES+=("openagent-computer-control")
        elif [ "$APP" = "openagent-cli" ]; then
            [ -f "host-tools/bundle-manifest.json" ] || {
                echo "ERROR: $PWD/host-tools is incomplete" >&2; exit 1;
            }
            FILES+=("host-tools")
        fi
        ARCHIVE="${APP}-${VERSION}-${OS}-${ARCH}.tar.gz"
        tar czf "$ARCHIVE" "${FILES[@]}"
        sha256 "$ARCHIVE" > "${ARCHIVE}.sha256"
        echo "✓ $ARCHIVE (+.sha256) with: ${FILES[*]}"
        ;;
    windows)
        BIN="${APP}.exe"
        [ -f "$BIN" ] || { echo "ERROR: $PWD/$BIN missing (PyInstaller output)" >&2; ls -la; exit 1; }
        # Same sidecar inclusion as linux. PowerShell's Compress-Archive
        # accepts a comma-separated path list via ``-Path``.
        FILES=("$BIN")
        if [ "$APP" = "openagent" ] && [ -f "openagent-computer-control.exe" ]; then
            FILES+=("openagent-computer-control.exe")
        elif [ "$APP" = "openagent-cli" ]; then
            [ -f "host-tools/bundle-manifest.json" ] || {
                echo "ERROR: $PWD/host-tools is incomplete" >&2; exit 1;
            }
            FILES+=("host-tools")
        fi
        ARCHIVE="${APP}-${VERSION}-${OS}-${ARCH}.zip"
        # Git Bash doesn't ship a ``zip`` binary on GHA's windows-latest,
        # but PowerShell's Compress-Archive is always there. One round-
        # trip is cheaper than installing a zip CLI.
        PS_PATHS=$(printf "'%s'," "${FILES[@]}")
        PS_PATHS="${PS_PATHS%,}"
        powershell.exe -NoProfile -Command "Compress-Archive -Path $PS_PATHS -DestinationPath '$ARCHIVE' -Force"
        sha256 "$ARCHIVE" > "${ARCHIVE}.sha256"
        echo "✓ $ARCHIVE (+.sha256) with: ${FILES[*]}"
        ;;
esac
