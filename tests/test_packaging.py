from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from importlib.metadata import version as distribution_version
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRANSPORT = ROOT / "src" / "openagent_client_transport"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_vendored_transport_lock_matches_committed_sources():
    lock = json.loads((TRANSPORT / "transport-source.json").read_text())
    assert lock["schema"] == 1
    assert len(lock["source_commit"]) == 40
    for relative, expected in lock["generated_files"].items():
        path = TRANSPORT / relative
        assert path.is_file(), relative
        assert _sha256(path) == expected, relative


def test_vendored_transport_matches_sibling_server_when_available():
    server = ROOT.parent / "openagent-server"
    if not server.is_dir():
        return
    lock = json.loads((TRANSPORT / "transport-source.json").read_text())
    for relative, expected in lock["source_files"].items():
        path = server / relative
        assert path.is_file(), relative
        assert _sha256(path) == expected, (
            f"{relative} drifted; rerun scripts/vendor-client-transport.py "
            "after reviewing the client/server wire change"
        )


def test_cli_owns_no_top_level_src_distribution_package():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'include = ["openagent_cli*", "openagent_client_transport*"]' in pyproject
    assert not (ROOT / "src" / "__init__.py").exists()
    assert not (ROOT / "src" / "main.py").exists()
    assert not (ROOT / "src" / "client.py").exists()


def test_pyinstaller_script_invokes_the_cli_entrypoint():
    entrypoint = (ROOT / "src" / "openagent_cli" / "main.py").read_text()
    assert 'if __name__ == "__main__":' in entrypoint
    assert "    main()" in entrypoint


def test_release_acquisition_verifies_consumer_owned_bundle_and_wheel_hashes(tmp_path):
    platform = "linux-x64"
    staged = tmp_path / platform
    staged.mkdir()
    executable = staged / "openagent-host-tools"
    executable.write_bytes(b"host-binary")
    manifest = {
        "manifest_version": 1,
        "version": "0.1.0",
        "platform": platform,
        "files": {
            executable.name: {
                "size": executable.stat().st_size,
                "sha256": _sha256(executable),
            }
        },
    }
    manifest_path = staged / "bundle-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    archive = tmp_path / f"openagent-host-tools-{platform}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(staged, arcname=platform)
    wheel = tmp_path / "openagent_host_tools-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    lock = tmp_path / "host-tools.lock.json"
    lock.write_text(json.dumps({
        "schema": 1,
        "version": "0.1.0",
        "source_repository": "openagent-uno/openagent-host-tools",
        "source_ref": "v0.1.0",
        "source_commit": "a" * 40,
        "python_wheel": {"asset": wheel.name, "sha256": _sha256(wheel)},
        "platforms": {
            platform: {
                "asset": archive.name,
                "archive_sha256": _sha256(archive),
                "bundle_manifest_sha256": _sha256(manifest_path),
            }
        },
    }))
    output = tmp_path / "verified"
    wheel_output = tmp_path / "verified.whl"
    completed = subprocess.run([
        sys.executable, str(ROOT / "scripts" / "acquire-host-tools.py"),
        "--lock", str(lock), "--platform", platform,
        "--output", str(output), "--archive", str(archive),
        "--wheel-output", str(wheel_output), "--wheel", str(wheel),
    ], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert (output / platform / "openagent-host-tools").read_bytes() == b"host-binary"
    assert wheel_output.read_bytes() == b"wheel"

    manifest["files"][executable.name]["sha256"] = "0" * 64
    # Archive hash pins the entire original archive, so corrupting the archive
    # itself is rejected before its self-described manifest can be trusted.
    archive.write_bytes(archive.read_bytes() + b"tampered")
    failed = subprocess.run([
        sys.executable, str(ROOT / "scripts" / "acquire-host-tools.py"),
        "--lock", str(lock), "--platform", platform,
        "--output", str(tmp_path / "rejected"), "--archive", str(archive),
    ], capture_output=True, text=True, check=False)
    assert failed.returncode != 0
    assert "archive SHA-256" in failed.stderr

    linked_archive = tmp_path / "linked.tar.gz"
    with tarfile.open(linked_archive, "w:gz") as bundle:
        root = tarfile.TarInfo(platform)
        root.type = tarfile.DIRTYPE
        bundle.addfile(root)
        link = tarfile.TarInfo(f"{platform}/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        bundle.addfile(link)
    linked_lock = json.loads(lock.read_text())
    linked_lock["platforms"][platform]["asset"] = linked_archive.name
    linked_lock["platforms"][platform]["archive_sha256"] = _sha256(linked_archive)
    lock.write_text(json.dumps(linked_lock))
    linked = subprocess.run([
        sys.executable, str(ROOT / "scripts" / "acquire-host-tools.py"),
        "--lock", str(lock), "--platform", platform,
        "--output", str(tmp_path / "linked-rejected"),
        "--archive", str(linked_archive),
    ], capture_output=True, text=True, check=False)
    assert linked.returncode != 0
    assert "regular files/directories" in linked.stderr


def test_release_acquisition_rejects_a_manifest_for_another_platform(tmp_path):
    platform = "linux-x64"
    staged = tmp_path / platform
    staged.mkdir()
    executable = staged / "openagent-host-tools"
    executable.write_bytes(b"wrong-platform-host")
    manifest = {
        "manifest_version": 1,
        "version": "0.1.0",
        "platform": "darwin-arm64",
        "files": {
            executable.name: {
                "size": executable.stat().st_size,
                "sha256": _sha256(executable),
            }
        },
    }
    manifest_path = staged / "bundle-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    archive = tmp_path / f"openagent-host-tools-{platform}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(staged, arcname=platform)
    lock = tmp_path / "host-tools.lock.json"
    lock.write_text(json.dumps({
        "schema": 1,
        "version": "0.1.0",
        "source_repository": "openagent-uno/openagent-host-tools",
        "source_ref": "v0.1.0",
        "source_commit": "c" * 40,
        "platforms": {
            platform: {
                "asset": archive.name,
                "archive_sha256": _sha256(archive),
                "bundle_manifest_sha256": _sha256(manifest_path),
            }
        },
    }))
    completed = subprocess.run([
        sys.executable, str(ROOT / "scripts" / "acquire-host-tools.py"),
        "--lock", str(lock), "--platform", platform,
        "--output", str(tmp_path / "rejected"), "--archive", str(archive),
    ], capture_output=True, text=True, check=False)
    assert completed.returncode != 0
    assert "bundle identity" in completed.stderr


def test_current_host_producer_archive_is_accepted_when_available(tmp_path):
    """Exercise the real producer archive through the CLI's strict consumer.

    This catches archive-shape drift (notably npm ``.bin`` symlinks) that a
    synthetic one-file fixture cannot represent. Standalone CLI checkouts may
    not carry the sibling producer/dist tree, so the release workflow remains
    the mandatory version of this same contract.
    """
    producer = ROOT.parent / "openagent-host-tools"
    archives = sorted((producer / "dist").glob("openagent-host-tools-*-*.tar.gz"))
    archives = [
        path for path in archives
        if not path.name.startswith("openagent-host-tools-0.1.0-")
    ]
    if not archives:
        return
    archive = archives[0]
    platform = archive.name.removeprefix("openagent-host-tools-").removesuffix(".tar.gz")
    if platform not in {
        "darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64",
        "win32-arm64", "win32-x64",
    }:
        return
    manifest = producer / "dist" / platform / "bundle-manifest.json"
    assert manifest.is_file()
    lock = tmp_path / "producer-lock.json"
    lock.write_text(json.dumps({
        "schema": 1,
        "version": "0.1.0",
        "source_repository": "openagent-uno/openagent-host-tools",
        "source_ref": "v0.1.0",
        "source_commit": "b" * 40,
        "platforms": {
            platform: {
                "asset": archive.name,
                "archive_sha256": _sha256(archive),
                "bundle_manifest_sha256": _sha256(manifest),
            },
        },
    }))
    output = tmp_path / "producer-verified"
    completed = subprocess.run([
        sys.executable, str(ROOT / "scripts" / "acquire-host-tools.py"),
        "--lock", str(lock), "--platform", platform,
        "--output", str(output), "--archive", str(archive),
    ], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert (output / platform / "bundle-manifest.json").is_file()


def test_release_spec_fails_closed_without_a_pinned_native_bundle():
    spec = (ROOT / "cli.spec").read_text()
    assert 'os.environ.get("OPENAGENT_RELEASE_BUILD") == "1"' in spec
    assert "OPENAGENT_HOST_TOOLS_BUNDLE is required" in spec
    assert 'datas.append((str(_path), f"openagent_host_tools/bin/' not in spec
    assert "PyInstaller re-signs nested Mach-O" in spec


def test_committed_host_tools_lock_and_python_dependency_are_immutable():
    lock_path = ROOT / "host-tools.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert _sha256(lock_path) == "9bbfd7a094c8d1b3238e8a580caba768af7d6b455f42a41f05dd9bfc8d7f727f"
    assert lock["source_commit"] == "660225c8e8bbf6488173d4e6d4d1b3ba04e8f194"
    assert set(lock["platforms"]) == {
        "darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64",
        "win32-arm64", "win32-x64",
    }
    wheel = lock["python_wheel"]
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert wheel["asset"] in pyproject
    assert f"#sha256={wheel['sha256']}" in pyproject
    assert "[tool.uv.sources]" not in pyproject
    assert "../openagent-host-tools" not in pyproject
    for workflow_name in ("test.yml", "release.yml"):
        workflow = (
            ROOT / ".github" / "workflows" / workflow_name
        ).read_text(encoding="utf-8")
        assert wheel["asset"] in workflow
        assert "openagent-host-tools.whl" not in workflow


def test_frozen_entrypoint_discovers_external_host_tools_locations():
    entrypoint = (ROOT / "src" / "openagent_cli" / "main.py").read_text()
    assert 'executable_dir / "host-tools"' in entrypoint
    assert '"lib" / "openagent" / "host-tools"' in entrypoint
    assert 'os.environ["OPENAGENT_HOST_TOOLS_SIDECAR_DIR"]' in entrypoint


def test_frozen_smoke_enables_and_revokes_consent_around_catalog_probe():
    smoke = (ROOT / "scripts" / "smoke-frozen-local-tools.py").read_text()
    assert '"enable", "--yes"' in smoke
    assert '"status"' in smoke
    assert '"disable"' in smoke
    assert "_PASS_COUNT = 2" in smoke
    assert "_exercise_plugin_through_broker" in smoke
    assert "_require_broker_unavailable" in smoke


def test_linux_release_packaging_uses_cli_distribution_metadata_in_clean_checkout(tmp_path):
    """Run the real packager where importing ``src`` is guaranteed to fail."""
    checkout = tmp_path / "clean-checkout"
    scripts = checkout / "scripts"
    dist = checkout / "dist"
    poisoned_src = checkout / "src"
    scripts.mkdir(parents=True)
    dist.mkdir()
    poisoned_src.mkdir()
    shutil.copy2(ROOT / "scripts" / "package-release.sh", scripts)
    (dist / "openagent-cli").write_bytes(b"frozen-cli")
    native = checkout / "verified-host-tools"
    native.mkdir()
    (native / "bundle-manifest.json").write_text("{}", encoding="utf-8")
    (native / "openagent-capability-host").write_bytes(b"native-host")
    (poisoned_src / "__init__.py").write_text(
        "raise RuntimeError('release packager imported checkout src')\n",
        encoding="utf-8",
    )

    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHON": sys.executable,
        "PYTHONPATH": "",
        "RUNNER_OS": "Linux",
        "COPYFILE_DISABLE": "1",
        "OPENAGENT_HOST_TOOLS_BUNDLE": str(native),
    }
    completed = subprocess.run(
        ["bash", "scripts/package-release.sh", "openagent-cli", "dist"],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    archives = list(dist.glob("openagent-cli-*-linux-*.tar.gz"))
    assert len(archives) == 1
    assert archives[0].name.startswith(
        f"openagent-cli-{distribution_version('openagent-cli')}-linux-"
    )
    assert archives[0].with_suffix(archives[0].suffix + ".sha256").is_file()
    with tarfile.open(archives[0], "r:gz") as package:
        names = package.getnames()
        assert names[0] == "openagent-cli"
        assert "host-tools" in names
        assert "host-tools/bundle-manifest.json" in names
        assert "host-tools/openagent-capability-host" in names


def test_macos_release_fails_closed_and_reads_cli_distribution_version():
    signing = (ROOT / "scripts" / "sign-notarize-macos.sh").read_text()
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "OPENAGENT_REQUIRE_SIGNING" in signing
    assert "version('openagent-cli')" in signing
    assert 'OPENAGENT_REQUIRE_SIGNING: \'1\'' in workflow
    assert 'EXTRA_RUNTIME_DIR="${5:-}"' in signing
    assert "/usr/local/lib/openagent/host-tools" in signing
    assert "pkgutil --payload-files" in signing
    assert '"$OPENAGENT_HOST_TOOLS_BUNDLE"' in workflow
