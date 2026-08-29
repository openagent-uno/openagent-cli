from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_concurrent_first_creation_returns_persisted_winner(tmp_path: Path) -> None:
    identity_path = tmp_path / "identity.key"
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    worker_count = 8
    worker = r"""
import os
import sys
import time
from pathlib import Path

from openagent_client_transport.network import identity as identity_module

identity_path = Path(sys.argv[1])
barrier_dir = Path(sys.argv[2])
expected = int(sys.argv[3])
original_generate = identity_module.Identity.generate

@classmethod
def coordinated_generate(cls):
    # load_or_create_identity calls generate only after observing ENOENT. Hold
    # all processes here so the original overwrite race is deterministic.
    (barrier_dir / str(os.getpid())).touch(exist_ok=False)
    deadline = time.monotonic() + 15
    while len(tuple(barrier_dir.iterdir())) < expected:
        if time.monotonic() >= deadline:
            raise TimeoutError("identity race barrier timed out")
        time.sleep(0.005)
    return original_generate()

identity_module.Identity.generate = coordinated_generate
identity = identity_module.load_or_create_identity(identity_path)
print(identity.public_hex, flush=True)
"""
    env = os.environ.copy()
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker,
                str(identity_path),
                str(barrier_dir),
                str(worker_count),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(worker_count)
    ]

    results: list[str] = []
    try:
        for process in workers:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            results.append(stdout.strip())
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill()
                process.wait()

    assert len(set(results)) == 1
    assert identity_path.read_bytes().hex() != ""
    assert len(identity_path.read_bytes()) == 32
    assert not tuple(tmp_path.glob(".identity-*"))
    if os.name == "posix":
        assert stat.S_IMODE(identity_path.stat().st_mode) == 0o600
