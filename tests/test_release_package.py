from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "test-release-package.sh"
APP = "openagent-cli"
VERSION = "0.16.0-beta.1"
ARCHIVE_NAME = f"{APP}-{VERSION}-windows-x64.zip"
EXECUTABLE_NAME = f"{APP}.exe"
HOST_MANIFEST = "host-tools/bundle-manifest.json"
HOST_EXECUTABLE = "host-tools/openagent-capability-host.exe"
EXECUTABLE = b"""#!/bin/sh
case "${1-}" in
    --version) printf '%s\\n' 'openagent-cli, version 0.16.0-beta.1' ;;
    --help) exit 0 ;;
    server-info) [ "${2-}" = "--help" ] ;;
    *) exit 64 ;;
esac
"""


def _write_archive(root: Path, extra_entries: dict[str, bytes] | None = None) -> None:
    archive_path = root / ARCHIVE_NAME
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(EXECUTABLE_NAME, EXECUTABLE)
        archive.writestr(HOST_MANIFEST, b"{}")
        archive.writestr(HOST_EXECUTABLE, b"host")
        for name, payload in (extra_entries or {}).items():
            archive.writestr(name, payload)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (root / f"{ARCHIVE_NAME}.sha256").write_text(
        f"{digest}  {ARCHIVE_NAME}\n",
        encoding="utf-8",
    )


def _verify(root: Path) -> subprocess.CompletedProcess[str]:
    tools = root / "tools"
    tools.mkdir()
    tar = tools / "tar"
    tar.write_text(
        "#!/bin/sh\necho 'tar must not be used for a Windows ZIP' >&2\nexit 97\n",
        encoding="utf-8",
    )
    tar.chmod(0o755)
    uname = tools / "uname"
    uname.write_text("#!/bin/sh\nprintf '%s\\n' x86_64\n", encoding="utf-8")
    uname.chmod(0o755)
    python = tools / "python"
    python.write_text(
        "#!/bin/sh\n"
        "case \"${2-}\" in\n"
        "  *platform.machine*) printf '%s\\n' AMD64; exit 0 ;;\n"
        "esac\n"
        f"exec {shlex.quote(str(Path(sys.executable).resolve()))} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    env = os.environ.copy()
    env["RUNNER_OS"] = "Windows"
    env["PATH"] = f"{tools}{os.pathsep}{env['PATH']}"
    env["TMPDIR"] = str(root)
    return subprocess.run(
        ["bash", str(VERIFY), APP, str(root), VERSION],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class WindowsReleasePackageTests(unittest.TestCase):
    def test_verifies_zip_without_using_tar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_archive(root)

            result = _verify(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"verified final release package: {ARCHIVE_NAME}", result.stdout)

    def test_rejects_an_unexpected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_archive(root, {"notes.txt": b"not part of the release"})

            result = _verify(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected ZIP entry", result.stderr)

    def test_rejects_a_missing_host_tools_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / ARCHIVE_NAME
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(EXECUTABLE_NAME, EXECUTABLE)
                archive.writestr(HOST_EXECUTABLE, b"host")
            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            (root / f"{ARCHIVE_NAME}.sha256").write_text(
                f"{digest}  {ARCHIVE_NAME}\n",
                encoding="utf-8",
            )

            result = _verify(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing ZIP entries", result.stderr)

    def test_rejects_a_path_traversal_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_archive(root, {"../escape.txt": b"must not escape"})

            result = _verify(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe ZIP entry", result.stderr)


if __name__ == "__main__":
    unittest.main()
