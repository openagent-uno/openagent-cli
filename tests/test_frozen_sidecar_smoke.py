from __future__ import annotations

import base64
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke-frozen-local-tools.py"
SPEC = importlib.util.spec_from_file_location("frozen_sidecar_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def _text_result(text: str, *, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def test_computer_smoke_accepts_and_validates_real_cursor_and_png_results():
    cursor = _text_result('{"x":12,"y":34}')
    assert smoke._validate_computer_result(cursor, "get_cursor_position") == "granted"

    # A small valid PNG is enough to verify that binary MCP content survived
    # the frozen broker instead of being flattened into text.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    screenshot = {
        "content": [
            {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()}
        ],
        "isError": False,
    }
    assert smoke._validate_computer_result(screenshot, "get_screenshot") == "granted"


def test_computer_smoke_accepts_only_stable_macos_tcc_denials(monkeypatch):
    monkeypatch.setattr(smoke.sys, "platform", "darwin")
    denied = _text_result(
        "InputController init: macOS Accessibility permission required",
        is_error=True,
    )
    assert (
        smoke._validate_computer_result(denied, "get_cursor_position")
        == "expected-os-denial"
    )

    with pytest.raises(RuntimeError, match="unexpected computer-control"):
        smoke._validate_computer_result(
            _text_result("native helper crashed", is_error=True),
            "get_cursor_position",
        )


def test_linux_with_advertised_display_must_not_downgrade_errors(monkeypatch):
    monkeypatch.setattr(smoke.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    assert smoke._allowed_computer_denials("get_screenshot") == ()


def test_headless_linux_has_an_explicit_display_denial_allowlist(monkeypatch):
    monkeypatch.setattr(smoke.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    denied = _text_result("InputController init: enigo init failed", is_error=True)
    assert (
        smoke._validate_computer_result(denied, "get_cursor_position")
        == "expected-os-denial"
    )


def test_missing_chrome_is_allowed_only_for_opted_in_linux_arm64(monkeypatch):
    missing = _text_result(
        "Error: Could not launch any Chromium-based browser (tried: none). "
        "Install Google Chrome or set OPENAGENT_CHROME_BINARY.",
        is_error=True,
    )
    monkeypatch.setenv("OPENAGENT_RELEASE_ALLOW_MISSING_CHROME", "1")
    monkeypatch.setattr(smoke.sys, "platform", "linux")
    monkeypatch.setattr(smoke.platform, "machine", lambda: "aarch64")
    assert smoke._is_expected_missing_chrome(missing) is True

    monkeypatch.delenv("OPENAGENT_RELEASE_ALLOW_MISSING_CHROME")
    assert smoke._is_expected_missing_chrome(missing) is False
    monkeypatch.setenv("OPENAGENT_RELEASE_ALLOW_MISSING_CHROME", "1")
    monkeypatch.setattr(smoke.platform, "machine", lambda: "x86_64")
    assert smoke._is_expected_missing_chrome(missing) is False


def test_missing_chrome_gate_rejects_found_or_broken_browser(monkeypatch):
    monkeypatch.setenv("OPENAGENT_RELEASE_ALLOW_MISSING_CHROME", "1")
    monkeypatch.setattr(smoke.sys, "platform", "linux")
    monkeypatch.setattr(smoke.platform, "machine", lambda: "arm64")
    found_but_broken = _text_result(
        "Could not launch any Chromium-based browser (tried: chromium). "
        "Last error: process crashed",
        is_error=True,
    )
    assert smoke._is_expected_missing_chrome(found_but_broken) is False


def test_frozen_smoke_dispatches_both_sidecars_with_network_binding_and_cleanup():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '_exercise_computer_control(client, principal, pass_number)' in source
    assert 'screenshot=skipped-after-os-denial' in source
    assert 'server="agent-in-chrome"' in source
    assert 'tool="navigate"' in source
    assert 'tool="get_page_text"' in source
    assert "_is_expected_missing_chrome(context)" in source
    assert '"network_id": "frozen-cli-network"' in source
    assert 'home = Path(root) / "client-home"' in source
    assert "_spawn_windows_job_process" in source
    assert "windows_job.terminate(timeout=15)" in source
    assert "_require_windows_profiles_unlocked(self._home)" in source
    assert '"release_principal"' in source
    assert "page_server.wait_closed()" in source


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
def test_windows_job_kills_only_its_detached_tree_and_allows_a_clean_second_pass(
    tmp_path,
):
    """Exercise the kernel ownership primitive used around the frozen broker.

    The grandchild uses the same detached flags as Chromium and holds a file
    open. An unrelated process with the same Python image must survive both Job
    Object terminations. Reusing the owned path for pass two proves the first
    tree released its handle rather than merely hiding a stale process.
    """

    hold_open = (
        "import os,pathlib,sys,time\n"
        "handle=open(sys.argv[1],'w')\n"
        "handle.write(str(os.getpid()))\n"
        "handle.flush()\n"
        "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()),encoding='utf-8')\n"
        "time.sleep(120)\n"
    )
    spawn_detached = (
        "import subprocess,sys\n"
        "flags=getattr(subprocess,'DETACHED_PROCESS',0) | "
        "getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)\n"
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],"
        "sys.argv[3]],creationflags=flags)\n"
        "raise SystemExit(child.wait())\n"
    )

    def wait_ready(path: Path) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if path.is_file():
                return
            time.sleep(0.01)
        raise RuntimeError(f"process tree did not create readiness marker: {path}")

    unrelated_lock = tmp_path / "unrelated.lock"
    unrelated_ready = tmp_path / "unrelated.ready"
    unrelated = subprocess.Popen(
        [sys.executable, "-c", hold_open, str(unrelated_lock), str(unrelated_ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_ready(unrelated_ready)
        owned_lock = tmp_path / "owned.lock"
        for pass_number in range(1, 3):
            ready = tmp_path / f"owned-{pass_number}.ready"
            process = None
            job = None
            try:
                process, job = smoke._spawn_windows_job_process(
                    [
                        sys.executable,
                        "-c",
                        spawn_detached,
                        hold_open,
                        str(owned_lock),
                        str(ready),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                wait_ready(ready)
                job.terminate(timeout=15)
                process.wait(timeout=5)
                assert unrelated.poll() is None
                # Windows would reject this unlink while the detached holder
                # still owned the profile-style file handle.
                owned_lock.unlink()
            finally:
                if job is not None:
                    try:
                        if process is not None and process.poll() is None:
                            job.terminate(timeout=15)
                            process.wait(timeout=5)
                    finally:
                        job.close()
    finally:
        if unrelated.poll() is None:
            unrelated.terminate()
            try:
                unrelated.wait(timeout=5)
            except subprocess.TimeoutExpired:
                unrelated.kill()
                unrelated.wait(timeout=5)
