from __future__ import annotations

import base64
import importlib.util
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


def test_frozen_smoke_dispatches_both_sidecars_with_network_binding_and_cleanup():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '_exercise_computer_control(client, principal, pass_number)' in source
    assert 'screenshot=skipped-after-os-denial' in source
    assert 'server="agent-in-chrome"' in source
    assert 'tool="navigate"' in source
    assert 'tool="get_page_text"' in source
    assert '"network_id": f"frozen-cli-network-{pass_number}"' in source
    assert '"release_principal"' in source
    assert "page_server.wait_closed()" in source
