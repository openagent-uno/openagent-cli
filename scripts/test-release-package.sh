#!/usr/bin/env bash
set -euo pipefail

# Verify the exact final CLI package that the release job is about to upload.
# This runs after signing/packaging, checks the adjacent SHA-256, extracts the
# package into an isolated temporary directory, and launches that extracted
# binary. It never reads a developer checkout through PYTHONPATH.

release_app="${1:-openagent-cli}"
release_dist="${2:-dist}"
release_expected_version="${3:?usage: $0 <app> <dist-dir> <expected-version>}"

case "${RUNNER_OS:-$(uname -s)}" in
    macOS|Darwin)
        release_os="macos"
        release_extension="pkg"
        ;;
    Linux)
        release_os="linux"
        release_extension="tar.gz"
        ;;
    Windows|MINGW*|CYGWIN*|MSYS*)
        release_os="windows"
        release_extension="zip"
        ;;
    *)
        echo "unsupported release OS: ${RUNNER_OS:-$(uname -s)}" >&2
        exit 2
        ;;
esac

release_arch_raw="$(uname -m)"
case "$release_arch_raw" in
    x86_64|amd64)  release_arch="x64" ;;
    aarch64|arm64) release_arch="arm64" ;;
    *)             release_arch="$release_arch_raw" ;;
esac

release_dist="$(cd "$release_dist" && pwd)"
release_filename="${release_app}-${release_expected_version}-${release_os}-${release_arch}.${release_extension}"
release_archive="${release_dist}/${release_filename}"
release_checksum="${release_archive}.sha256"

[[ -f "$release_archive" ]] || {
    echo "missing final release package: $release_archive" >&2
    exit 1
}
[[ -f "$release_checksum" ]] || {
    echo "missing final release checksum: $release_checksum" >&2
    exit 1
}

(
    cd "$release_dist"
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -c "${release_filename}.sha256"
    else
        sha256sum -c "${release_filename}.sha256"
    fi
)

release_tmp="$(mktemp -d "${TMPDIR:-/tmp}/openagent-cli-release.XXXXXX")"
trap 'rm -rf -- "$release_tmp"' EXIT

case "$release_os" in
    macos)
        pkgutil --expand-full "$release_archive" "$release_tmp/pkg"
        release_candidates="$release_tmp/candidates.txt"
        find "$release_tmp/pkg" -type f -name "$release_app" -print > "$release_candidates"
        release_count="$(wc -l < "$release_candidates" | tr -d ' ')"
        [[ "$release_count" == "1" ]] || {
            echo "expected one $release_app in pkg, found $release_count" >&2
            exit 1
        }
        IFS= read -r release_binary < "$release_candidates"
        release_manifest_candidates="$release_tmp/manifest-candidates.txt"
        find "$release_tmp/pkg" -type f \
            -path '*/usr/local/lib/openagent/host-tools/bundle-manifest.json' \
            -print > "$release_manifest_candidates"
        release_manifest_count="$(wc -l < "$release_manifest_candidates" | tr -d ' ')"
        [[ "$release_manifest_count" == "1" ]] || {
            echo "expected one host-tools manifest in pkg, found $release_manifest_count" >&2
            exit 1
        }
        IFS= read -r release_host_manifest < "$release_manifest_candidates"
        ;;
    linux)
        python - "$release_archive" "$release_tmp" "$release_app" <<'PY'
import sys
import tarfile
from pathlib import Path, PurePosixPath


archive_path, destination, expected_name = sys.argv[1:]
with tarfile.open(archive_path, "r:gz") as archive:
    members = archive.getmembers()
    names: set[str] = set()
    for member in members:
        name = member.name.rstrip("/")
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != name
        ):
            raise SystemExit(f"unsafe tar entry: {member.name!r}")
        if not (member.isfile() or member.isdir()):
            raise SystemExit(f"unsupported tar entry: {member.name!r}")
        if name in names:
            raise SystemExit(f"duplicate tar entry: {member.name!r}")
        names.add(name)
        if name != expected_name and name != "host-tools" and not name.startswith(
            "host-tools/"
        ):
            raise SystemExit(f"unexpected tar entry: {member.name!r}")

    required = {expected_name, "host-tools/bundle-manifest.json"}
    if not required.issubset(names):
        raise SystemExit(f"missing tar entries: {sorted(required - names)!r}")
    archive.extractall(destination, members=members)
PY
        release_binary="$release_tmp/$release_app"
        release_host_manifest="$release_tmp/host-tools/bundle-manifest.json"
        ;;
    windows)
        python - "$release_archive" "$release_tmp" "${release_app}.exe" <<'PY'
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath


archive_path, destination, expected_name = sys.argv[1:]
with zipfile.ZipFile(archive_path) as archive:
    corrupt_entry = archive.testzip()
    if corrupt_entry is not None:
        raise SystemExit(f"corrupt ZIP entry: {corrupt_entry!r}")

    files: list[str] = []
    seen: set[str] = set()
    expected_entry = None
    for entry in archive.infolist():
        normalized = entry.filename.replace("\\", "/")
        name = normalized.rstrip("/")
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != name
        ):
            raise SystemExit(f"unsafe ZIP entry: {entry.filename!r}")
        if name in seen:
            raise SystemExit(f"duplicate ZIP entry: {entry.filename!r}")
        seen.add(name)
        if name != expected_name and name != "host-tools" and not name.startswith(
            "host-tools/"
        ):
            raise SystemExit(f"unexpected ZIP entry: {entry.filename!r}")
        if entry.is_dir():
            continue
        files.append(name)
        if name == expected_name:
            expected_entry = entry

    required = {expected_name, "host-tools/bundle-manifest.json"}
    if not required.issubset(files):
        raise SystemExit(f"missing ZIP entries: {sorted(required - set(files))!r}")
    if expected_entry is None:
        raise SystemExit(f"missing ZIP entry: {expected_name!r}")

    for entry in archive.infolist():
        normalized = entry.filename.replace("\\", "/").rstrip("/")
        target = Path(destination) / PurePosixPath(normalized)
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(entry) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
PY
        release_binary="$release_tmp/${release_app}.exe"
        release_host_manifest="$release_tmp/host-tools/bundle-manifest.json"
        chmod +x "$release_binary"
        ;;
esac

[[ -f "$release_binary" ]] || {
    echo "final package does not contain $release_app" >&2
    exit 1
}
[[ -x "$release_binary" ]] || {
    echo "extracted CLI is not executable: $release_binary" >&2
    exit 1
}
[[ -f "$release_host_manifest" ]] || {
    echo "final package does not contain the host-tools manifest" >&2
    exit 1
}

release_version_output="$("$release_binary" --version)"
release_expected_output="${release_app}, version ${release_expected_version}"
[[ "$release_version_output" == "$release_expected_output" ]] || {
    echo "version mismatch: expected '$release_expected_output', got '$release_version_output'" >&2
    exit 1
}
"$release_binary" --help >/dev/null
"$release_binary" server-info --help >/dev/null

echo "verified final release package: $release_filename"
