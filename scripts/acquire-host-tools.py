#!/usr/bin/env python3
"""Acquire and verify the exact native host-tools bundle pinned by the CLI.

The lock contains a detached archive digest and the expected digest of the
bundle's internal manifest.  Both are consumer-owned values: neither is read
from the downloaded release itself.  Every file is then checked against the
verified internal manifest before PyInstaller is allowed to embed it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str, platform_key: str) -> None:
    value = PurePosixPath(name.replace("\\", "/"))
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise SystemExit(f"unsafe host-tools archive member: {name!r}")
    if value.parts[0] != platform_key:
        raise SystemExit(
            f"host-tools archive member is outside {platform_key!r}: {name!r}"
        )


def extract(archive: Path, output: Path, platform_key: str) -> None:
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"host-tools output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                _safe_member(member.name, platform_key)
                if not (member.isdir() or member.isfile()):
                    raise SystemExit(
                        "host-tools release archives may contain only regular "
                        f"files/directories: {member.name}"
                    )
            bundle.extractall(output, filter="data")
        return
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                _safe_member(member.filename, platform_key)
                mode = (member.external_attr >> 16) & 0xFFFF
                if not member.is_dir() and mode and not stat.S_ISREG(mode):
                    raise SystemExit(
                        "host-tools release archives may contain only regular "
                        f"files/directories: {member.filename}"
                    )
            bundle.extractall(output)
        return
    raise SystemExit(f"unsupported host-tools archive: {archive.name}")


def download(url: str, destination: Path) -> None:
    headers = {"User-Agent": "openagent-cli-release"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)


def verify_bundle(
    bundle: Path,
    expected_manifest_sha256: str,
    version: str,
    platform_key: str,
) -> None:
    manifest_path = bundle / "bundle-manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"host-tools bundle manifest is missing: {manifest_path}")
    if sha256(manifest_path) != expected_manifest_sha256:
        raise SystemExit("host-tools bundle manifest SHA-256 does not match the consumer lock")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("manifest_version") != 1
        or manifest.get("version") != version
        or manifest.get("platform") != platform_key
        or bundle.name != platform_key
    ):
        raise SystemExit("host-tools bundle identity does not match the lock")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise SystemExit("host-tools bundle manifest has no file map")
    root = bundle.resolve()
    actual_files = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path.name != "bundle-manifest.json"
    }
    if actual_files != set(files):
        raise SystemExit("host-tools bundle file set does not match its manifest")
    for relative, metadata in files.items():
        if not isinstance(relative, str) or not isinstance(metadata, dict):
            raise SystemExit("host-tools bundle manifest contains an invalid file entry")
        path = (bundle / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise SystemExit(f"unsafe or missing host-tools bundle file: {relative}")
        if path.stat().st_size != metadata.get("size") or sha256(path) != metadata.get("sha256"):
            raise SystemExit(f"host-tools bundle integrity mismatch: {relative}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=Path("host-tools.lock.json"))
    parser.add_argument("--platform", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, help="test/offline archive override")
    parser.add_argument("--wheel-output", type=Path, help="also acquire the pinned Python wheel")
    parser.add_argument("--wheel", type=Path, help="test/offline wheel override")
    args = parser.parse_args()
    if args.platform not in {
        "darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64",
        "win32-arm64", "win32-x64",
    }:
        raise SystemExit(f"unsupported host-tools platform: {args.platform}")
    lock: dict[str, Any] = json.loads(args.lock.read_text(encoding="utf-8"))
    platform = (lock.get("platforms") or {}).get(args.platform)
    if lock.get("schema") != 1 or not isinstance(platform, dict):
        raise SystemExit("host-tools lock has no exact platform entry")
    version = str(lock.get("version") or "")
    source_ref = str(lock.get("source_ref") or "")
    source_commit = str(lock.get("source_commit") or "")
    repository = str(lock.get("source_repository") or "")
    asset = str(platform.get("asset") or "")
    archive_digest = str(platform.get("archive_sha256") or "")
    manifest_digest = str(platform.get("bundle_manifest_sha256") or "")
    if (
        source_ref != f"v{version}"
        or not repository
        or len(source_commit) != 40
        or any(char not in "0123456789abcdef" for char in source_commit)
        or Path(asset).name != asset
        or len(archive_digest) != 64
        or any(char not in "0123456789abcdef" for char in archive_digest)
        or len(manifest_digest) != 64
        or any(char not in "0123456789abcdef" for char in manifest_digest)
    ):
        raise SystemExit("host-tools lock is incomplete or not immutable")
    output = args.output.resolve()
    archive = args.archive.resolve() if args.archive else output.parent / asset
    if not args.archive:
        url = f"https://github.com/{repository}/releases/download/{source_ref}/{asset}"
        download(url, archive)
    if sha256(archive) != archive_digest:
        raise SystemExit("host-tools release archive SHA-256 does not match the consumer lock")
    extract(archive, output, args.platform)
    bundle = output / args.platform
    verify_bundle(bundle, manifest_digest, version, args.platform)
    if args.wheel_output:
        wheel_lock = lock.get("python_wheel")
        if not isinstance(wheel_lock, dict):
            raise SystemExit("host-tools lock has no pinned Python wheel")
        wheel_asset = str(wheel_lock.get("asset") or "")
        wheel_digest = str(wheel_lock.get("sha256") or "")
        if (
            Path(wheel_asset).name != wheel_asset
            or not wheel_asset.endswith(".whl")
            or len(wheel_digest) != 64
            or any(char not in "0123456789abcdef" for char in wheel_digest)
        ):
            raise SystemExit("host-tools Python wheel lock is incomplete")
        wheel_output = args.wheel_output.resolve()
        wheel_output.parent.mkdir(parents=True, exist_ok=True)
        wheel_source = args.wheel.resolve() if args.wheel else wheel_output
        if not args.wheel:
            wheel_url = (
                f"https://github.com/{repository}/releases/download/"
                f"{source_ref}/{wheel_asset}"
            )
            download(wheel_url, wheel_output)
        elif wheel_source != wheel_output:
            shutil.copy2(wheel_source, wheel_output)
        if sha256(wheel_output) != wheel_digest:
            raise SystemExit("host-tools Python wheel SHA-256 does not match the consumer lock")
        print(wheel_output)
    print(bundle)


if __name__ == "__main__":
    main()
