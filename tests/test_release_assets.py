from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "scripts" / "verify-release-assets.py"
VERSION = "0.16.0-beta.1"
BASE = f"openagent-cli-{VERSION}"
PRIMARY = (
    f"{BASE}-linux-x64.tar.gz",
    f"{BASE}-linux-arm64.tar.gz",
    f"{BASE}-macos-x64.pkg",
    f"{BASE}-macos-arm64.pkg",
    f"{BASE}-windows-x64.zip",
    f"{BASE}-windows-arm64.zip",
)


def _artifacts(root: Path) -> None:
    for name in PRIMARY:
        payload = f"fixture:{name}".encode()
        (root / name).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        (root / f"{name}.sha256").write_text(
            f"{digest}  {name}\n",
            encoding="utf-8",
        )


def _verify(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFY), str(root), VERSION],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class ReleaseAssetVerificationTests(unittest.TestCase):
    def test_accepts_gnu_binary_marker_and_crlf_from_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _artifacts(root)
            target = f"{BASE}-windows-x64.zip"
            digest = hashlib.sha256((root / target).read_bytes()).hexdigest()
            (root / f"{target}.sha256").write_bytes(
                f"{digest} *{target}\r\n".encode()
            )

            result = _verify(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified 12 immutable CLI release assets", result.stdout)

    def test_rejects_target_hidden_in_nested_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _artifacts(root)
            target = f"{BASE}-windows-x64.zip"
            digest = hashlib.sha256((root / target).read_bytes()).hexdigest()
            (root / f"{target}.sha256").write_text(
                f"{digest}  nested/{target}\n",
                encoding="utf-8",
            )

            result = _verify(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid checksum sidecar", result.stderr)


if __name__ == "__main__":
    unittest.main()
