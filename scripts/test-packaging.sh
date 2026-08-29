#!/usr/bin/env bash
set -euo pipefail

# Build and exercise the distribution from an isolated source copy.  This is
# intentionally different from an editable install: it catches missing package
# files, accidental ``src`` modules, and PyInstaller-only import failures.

packaging_mode="${1:-full}"
packaging_python="${OPENAGENT_PACKAGING_PYTHON:-3.12}"

if [[ "$packaging_mode" != "full" && "$packaging_mode" != "wheel-only" ]]; then
  echo "usage: $0 [full|wheel-only]" >&2
  exit 2
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required for the packaging smoke test" >&2
  exit 2
fi

packaging_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
packaging_tmp="$(mktemp -d "${TMPDIR:-/tmp}/openagent-cli-package.XXXXXX")"
trap 'rm -rf -- "$packaging_tmp"' EXIT

mkdir -p "$packaging_tmp/source"
cp "$packaging_root/pyproject.toml" "$packaging_root/cli.spec" "$packaging_tmp/source/"
mkdir -p "$packaging_tmp/source/src" "$packaging_tmp/source/scripts"
cp -R "$packaging_root/src/openagent_cli" "$packaging_tmp/source/src/"
cp -R "$packaging_root/src/openagent_client_transport" "$packaging_tmp/source/src/"
cp "$packaging_root/scripts/cli_entry.py" "$packaging_tmp/source/scripts/"

cd "$packaging_tmp/source"
uv build --wheel --out-dir "$packaging_tmp/wheels"

packaging_wheels=("$packaging_tmp"/wheels/*.whl)
if [[ ${#packaging_wheels[@]} -ne 1 ]]; then
  echo "expected exactly one wheel, found ${#packaging_wheels[@]}" >&2
  exit 1
fi
packaging_wheel="${packaging_wheels[0]}"
packaging_contents="$packaging_tmp/wheel-contents.txt"
unzip -Z1 "$packaging_wheel" > "$packaging_contents"

for packaging_required in \
  openagent_cli/main.py \
  openagent_cli/client.py \
  openagent_cli/remote_api.py \
  openagent_cli/network/client/session.py \
  openagent_client_transport/transport-source.json \
  openagent_client_transport/network/client/session.py; do
  grep -Fxq "$packaging_required" "$packaging_contents"
done
if grep -Eq '^(__init__|main|client|remote_api)\.py$' "$packaging_contents"; then
  echo "wheel leaked legacy top-level src modules" >&2
  exit 1
fi

uv venv --python "$packaging_python" "$packaging_tmp/venv"
uv pip install --python "$packaging_tmp/venv/bin/python" "$packaging_wheel"
uv pip check --python "$packaging_tmp/venv/bin/python"

mkdir -p "$packaging_tmp/run"
cd "$packaging_tmp/run"
packaging_expected_version="$($packaging_tmp/venv/bin/python -c 'from openagent_cli import __version__; print(__version__)')"
[[ "$("$packaging_tmp/venv/bin/openagent-cli" --version)" == \
  "openagent-cli, version $packaging_expected_version" ]]
"$packaging_tmp/venv/bin/openagent-cli" --help >/dev/null
"$packaging_tmp/venv/bin/openagent-cli" history --help >/dev/null
"$packaging_tmp/venv/bin/openagent-cli" search --help >/dev/null
"$packaging_tmp/venv/bin/python" - "$packaging_tmp/venv" <<'PY'
import pathlib
import sys

import openagent_cli
import openagent_cli.client
import openagent_cli.main
import openagent_cli.network.client.session
import openagent_cli.remote_api
import openagent_client_transport
import openagent_client_transport.network.client.session

venv = pathlib.Path(sys.argv[1]).resolve()
package_file = pathlib.Path(openagent_cli.__file__).resolve()
assert package_file.is_relative_to(venv), (package_file, venv)
assert package_file.name == "__init__.py"
PY

if [[ "$packaging_mode" == "wheel-only" ]]; then
  echo "wheel/install smoke passed on Python $packaging_python"
  exit 0
fi

uv pip install --python "$packaging_tmp/venv/bin/python" pyinstaller
cd "$packaging_tmp/source"
"$packaging_tmp/venv/bin/pyinstaller" \
  --clean \
  --noconfirm \
  --distpath "$packaging_tmp/frozen-dist" \
  --workpath "$packaging_tmp/frozen-build" \
  cli.spec

cd "$packaging_tmp/run"
[[ "$("$packaging_tmp/frozen-dist/openagent-cli" --version)" == \
  "openagent-cli, version $packaging_expected_version" ]]
"$packaging_tmp/frozen-dist/openagent-cli" --help >/dev/null
"$packaging_tmp/frozen-dist/openagent-cli" server-info --help >/dev/null
echo "wheel/install/frozen-binary smoke passed on Python $packaging_python"
