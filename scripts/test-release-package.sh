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
        ;;
    linux)
        tar -xzf "$release_archive" -C "$release_tmp"
        release_binary="$release_tmp/$release_app"
        ;;
    windows)
        tar -xf "$release_archive" -C "$release_tmp"
        release_binary="$release_tmp/${release_app}.exe"
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

release_version_output="$("$release_binary" --version)"
release_expected_output="${release_app}, version ${release_expected_version}"
[[ "$release_version_output" == "$release_expected_output" ]] || {
    echo "version mismatch: expected '$release_expected_output', got '$release_version_output'" >&2
    exit 1
}
"$release_binary" --help >/dev/null
"$release_binary" server-info --help >/dev/null

echo "verified final release package: $release_filename"
