#!/usr/bin/env python3
"""Release smoke for the real frozen CLI and its packaged capability host."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


_PASS_COUNT = 2
_PLUGIN_NAME = "release-smoke-plugin"
_PLUGIN_SOURCE = r'''import json, os, sys
for line in sys.stdin:
    value = json.loads(line)
    request_id = value.get("id")
    if request_id is None:
        continue
    method = value.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "release-smoke-plugin", "version": "1.0.0"},
            "instructions": "Frozen release smoke plugin.",
        }
    elif method == "tools/list":
        result = {
            "tools": [{
                "name": "echo_marker",
                "description": "Return the supplied release-smoke marker.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"marker": {"type": "string"}},
                    "required": ["marker"],
                },
                "annotations": {"readOnlyHint": True},
            }]
        }
    elif method == "tools/call":
        marker = (value.get("params") or {}).get("arguments", {}).get("marker")
        result = {
            "content": [{"type": "text", "text": "plugin:" + str(marker)}],
            "structuredContent": {"marker": marker, "pid": os.getpid()},
        }
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
'''


def main() -> None:
    executable = Path(sys.argv[1]).resolve()
    if os.name == "nt" and executable.suffix.lower() != ".exe":
        executable = executable.with_suffix(".exe")
    if not executable.is_file():
        raise SystemExit(f"frozen CLI is missing: {executable}")
    summaries = []
    with tempfile.TemporaryDirectory(prefix="openagent-cli-smoke-") as root:
        for pass_number in range(1, _PASS_COUNT + 1):
            home = Path(root) / f"pass-{pass_number}"
            home.mkdir()
            env = os.environ.copy()
            env["OPENAGENT_HOST_TOOLS_HOME"] = str(home)
            # Release packaging stages the immutable bundle beside the executable.
            # Ignore the CI acquisition path so this smoke proves the distributable
            # layout rather than a build-machine override.
            env.pop("OPENAGENT_HOST_TOOLS_BUNDLE", None)
            env.pop("OPENAGENT_HOST_TOOLS_SIDECAR_DIR", None)
            _configure_plugin(home)
            summaries.append(_run_pass(executable, env, home, pass_number))
    print("\n\n".join(summaries))


def _run_pass(
    executable: Path,
    env: dict[str, str],
    home: Path,
    pass_number: int,
) -> str:
    broker = _FrozenBroker(executable, env, home)
    broker.start()
    try:
        enabled = _run(executable, env, "enable", "--yes")
        completed = _run(executable, env, "status")
        enable_output = enabled.stdout + "\n" + enabled.stderr
        output = completed.stdout + "\n" + completed.stderr
        if enabled.returncode != 0 or "Could not update local tools" in enable_output:
            raise RuntimeError(f"frozen local-tools enable failed:\n{enable_output}")
        if completed.returncode != 0 or "Could not read local tools status" in output:
            raise RuntimeError(f"frozen local-tools status failed:\n{output}")
        _validate_catalog_output(output)
        asyncio.run(_exercise_plugin_through_broker(home, pass_number))
        disabled = _run(executable, env, "disable")
        disable_output = disabled.stdout + "\n" + disabled.stderr
        if disabled.returncode != 0 or "Could not update local tools" in disable_output:
            raise RuntimeError(f"frozen local-tools disable failed:\n{disable_output}")
        asyncio.run(_require_disabled_broker(home, pass_number))
    finally:
        broker.close()

    # The direct entrypoint verifies that the same frozen package exposes the
    # embedded adapter with identical filesystem/editor/shell semantics. It is
    # deliberately run after the single-instance broker has been reaped so the
    # two hosts never contend for the durable ledger or mutation lease.
    _exercise_frozen_host(executable, env, home, pass_number)
    asyncio.run(_require_broker_unavailable(home))
    return f"Frozen local-tools release smoke pass {pass_number}/{_PASS_COUNT}:\n{output.strip()}"


def _validate_catalog_output(output: str) -> None:
    required = {
        "filesystem",
        "editor",
        "shell",
        "computer-control",
        "agent-in-chrome",
        _PLUGIN_NAME,
    }
    missing = sorted(name for name in required if name not in output)
    if missing:
        raise RuntimeError(f"frozen catalog is missing {missing}:\n{output}")
    # Every required module must be usable from the packaged bundle; merely
    # listing a sidecar with an unavailable reason is a failed release.
    available_count = len(re.findall(r"\bavailable\b", output.lower()))
    if available_count < len(required):
        raise RuntimeError(f"frozen catalog has unavailable modules:\n{output}")


def _configure_plugin(home: Path) -> None:
    plugin = home / "release-smoke-plugin.py"
    plugin.write_text(_PLUGIN_SOURCE, encoding="utf-8")
    # JSON string syntax is valid TOML basic-string syntax and safely escapes
    # Windows paths. The interpreter is the CI Python fixture by design; the
    # host and all broker/dispatch logic under test still come from the frozen
    # release artifact.
    config = (
        "# Explicit local MCP used only by the frozen release smoke.\n"
        "version = 1\n\n"
        "[[mcp]]\n"
        f"name = {json.dumps(_PLUGIN_NAME)}\n"
        f"command = [{json.dumps(sys.executable)}, {json.dumps(str(plugin))}]\n"
        "enabled = true\n"
    )
    (home / "client-mcps.toml").write_text(config, encoding="utf-8")


class _FrozenBroker:
    """Own and deterministically reap one broker from the frozen executable."""

    def __init__(self, executable: Path, env: dict[str, str], home: Path):
        self._executable = executable
        self._env = env
        self._home = home
        self._stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("frozen broker was started twice")
        self._process = subprocess.Popen(
            [str(self._executable), "--broker"],
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr,
            text=True,
            close_fds=True,
        )
        try:
            asyncio.run(_wait_for_broker(self._home, self._process))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        process = self._process
        self._process = None
        stop_error = None
        try:
            if process is not None and process.poll() is None:
                # Consent revocation has already closed every plugin/sidecar.
                # This signal is therefore scoped to the exact broker child
                # owned by this smoke; it cannot affect a user's shared host.
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait(timeout=5)
                    stop_error = RuntimeError(
                        "frozen broker ignored termination and required exact-child cleanup"
                    )
                    stop_error.__cause__ = exc
            asyncio.run(_require_broker_unavailable(self._home))
        finally:
            self._stderr.close()
        if stop_error is not None:
            raise stop_error

    def stderr_text(self) -> str:
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()


async def _wait_for_broker(
    home: Path,
    process: subprocess.Popen[str],
    *,
    timeout: float = 15.0,
) -> None:
    from openagent_host_tools import HostPaths
    from openagent_host_tools.local_broker import LocalBrokerClient

    paths = HostPaths.discover(home)
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"frozen broker exited before readiness ({process.returncode})")
        client = LocalBrokerClient(paths)
        try:
            await asyncio.wait_for(client.connect(), timeout=1)
            await client.close()
            return
        except (OSError, EOFError, ConnectionRefusedError, FileNotFoundError, TimeoutError) as exc:
            last_error = exc
            await client.close()
            await asyncio.sleep(0.05)
    raise RuntimeError(f"frozen broker did not become ready: {last_error}")


async def _require_broker_unavailable(
    home: Path,
    *,
    timeout: float = 5.0,
    stable_for: float = 0.75,
) -> None:
    from openagent_host_tools import HostPaths
    from openagent_host_tools.local_broker import LocalBrokerClient

    paths = HostPaths.discover(home)
    deadline = time.monotonic() + timeout
    unavailable_since: float | None = None
    while time.monotonic() < deadline:
        client = LocalBrokerClient(paths)
        try:
            await asyncio.wait_for(client.connect(), timeout=0.5)
        except (OSError, EOFError, ConnectionRefusedError, FileNotFoundError, TimeoutError):
            await client.close()
            unavailable_since = unavailable_since or time.monotonic()
            if time.monotonic() - unavailable_since >= stable_for:
                return
        else:
            await client.close()
            unavailable_since = None
        await asyncio.sleep(0.05)
    raise RuntimeError(f"a broker residue still accepts connections for {home}")


async def _broker_request(client, request_id: str, request_type: str, **fields) -> dict:
    await client.send({"id": request_id, "type": request_type, **fields})
    while True:
        response = await asyncio.wait_for(client.receive(), timeout=30)
        if response is None:
            raise RuntimeError(f"frozen broker disconnected during {request_type}")
        if str(response.get("id")) == request_id:
            return response


async def _exercise_plugin_through_broker(home: Path, pass_number: int) -> None:
    from openagent_host_tools import HostPaths
    from openagent_host_tools.local_broker import LocalBrokerClient

    client = LocalBrokerClient(HostPaths.discover(home))
    await client.connect()
    try:
        status = await _broker_request(client, "plugin-status", "status")
        plugin = next(
            (
                server
                for server in status.get("result", {}).get("servers", [])
                if server.get("name") == _PLUGIN_NAME
            ),
            None,
        )
        if not plugin or plugin.get("source") != "plugin" or not plugin.get("available"):
            raise RuntimeError(f"explicit plugin was not healthy in frozen broker: {plugin}")
        marker = f"PLUGIN-CONTENT-MUST-NOT-ENTER-AUDIT-{pass_number}"
        response = await _broker_request(
            client,
            f"plugin-call-{pass_number}",
            "call",
            server=_PLUGIN_NAME,
            tool="echo_marker",
            args={"marker": marker},
            principal=f"frozen-cli-release-smoke-{pass_number}",
            call_id=f"frozen-plugin-{pass_number}",
            idempotency_key=f"frozen-plugin-{pass_number}",
        )
        if response.get("ok") is not True:
            raise RuntimeError(f"explicit plugin call failed through frozen broker: {response}")
        result = response.get("result") or {}
        if (result.get("structuredContent") or {}).get("marker") != marker:
            raise RuntimeError(f"explicit plugin lost its structured result: {result}")
        if result.get("_meta", {}).get("openagent/location") != "client":
            raise RuntimeError(f"explicit plugin lost client execution location: {result}")
    finally:
        await client.close()


async def _require_disabled_broker(home: Path, pass_number: int) -> None:
    from openagent_host_tools import HostPaths
    from openagent_host_tools.local_broker import LocalBrokerClient

    client = LocalBrokerClient(HostPaths.discover(home))
    await client.connect()
    try:
        denied = await _broker_request(
            client,
            f"disabled-call-{pass_number}",
            "call",
            server="filesystem",
            tool="read_text_file",
            args={"path": str(home / "frozen-client-note.txt")},
            principal=f"frozen-cli-release-smoke-{pass_number}",
            call_id=f"frozen-after-disable-{pass_number}",
            idempotency_key=f"frozen-after-disable-{pass_number}",
        )
        if denied.get("ok") is not False or denied.get("error", {}).get(
            "code"
        ) != "consent_required":
            raise RuntimeError(f"frozen broker did not enforce CLI disable: {denied}")
    finally:
        await client.close()


class _DirectHost:
    """Sequential NDJSON client for the frozen CLI's private host entrypoint."""

    def __init__(self, executable: Path, env: dict[str, str]):
        self._stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self._process = subprocess.Popen(
            [str(executable), "--direct"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            bufsize=1,
        )
        if self._process.stdin is None or self._process.stdout is None:
            raise RuntimeError("frozen direct host did not expose stdio")
        self._frames: queue.Queue[dict | BaseException | None] = queue.Queue()
        self._sequence = 0
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self._process.stdout is not None
        try:
            for raw in self._process.stdout:
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    self._frames.put(RuntimeError(f"non-JSON frozen host output: {raw!r}"))
                    self._frames.put(exc)
                    return
                if isinstance(value, dict):
                    self._frames.put(value)
        except BaseException as exc:  # pragma: no cover - OS pipe failure
            self._frames.put(exc)
        finally:
            self._frames.put(None)

    def request(
        self,
        request_type: str,
        *,
        expect_ok: bool = True,
        timeout: float = 30.0,
        **fields,
    ) -> dict:
        self._sequence += 1
        request_id = f"frozen-smoke-{self._sequence}"
        frame = {"id": request_id, "type": request_type, **fields}
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(frame, separators=(",", ":")) + "\n")
        self._process.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"frozen host timed out handling {request_type}")
            try:
                response = self._frames.get(timeout=remaining)
            except queue.Empty as exc:
                raise RuntimeError(f"frozen host timed out handling {request_type}") from exc
            if response is None:
                raise RuntimeError(
                    f"frozen host exited during {request_type}: {self._stderr_text()}"
                )
            if isinstance(response, BaseException):
                raise RuntimeError(f"frozen host output failed: {response}") from response
            if str(response.get("id")) != request_id:
                # Background shell completion events are allowed to interleave
                # with request/response frames in the production protocol.
                continue
            if bool(response.get("ok")) is not expect_ok:
                raise RuntimeError(
                    f"unexpected frozen host response for {request_type}: {response}"
                )
            return response

    def close(self) -> None:
        try:
            if self._process.poll() is None:
                self.request("shutdown", timeout=15)
            if self._process.stdin is not None:
                self._process.stdin.close()
            self._process.wait(timeout=30)
            if self._process.returncode != 0:
                raise RuntimeError(
                    f"frozen host exited {self._process.returncode}: {self._stderr_text()}"
                )
        finally:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
            self._stderr.close()

    def _stderr_text(self) -> str:
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()


def _exercise_frozen_host(
    executable: Path,
    env: dict[str, str],
    home: Path,
    pass_number: int,
) -> None:
    """Use the actual packaged executable for filesystem/editor/shell E2E."""

    target = home / "frozen-client-note.txt"
    initial = f"alpha CLIENT-CONTENT-MUST-NOT-ENTER-AUDIT-{pass_number}"
    principal = f"frozen-cli-release-smoke-{pass_number}"
    host = _DirectHost(executable, env)
    consent_enabled = False
    try:
        initialized = host.request("initialize")["result"]
        if initialized.get("protocol") != "openagent-host-tools/1":
            raise RuntimeError(f"unexpected frozen host protocol: {initialized}")
        host.request("set_consent", enabled=True, consent_version=1)
        consent_enabled = True
        catalog = host.request("catalog")["result"].get("servers") or []
        available = {
            item.get("name")
            for item in catalog
            if isinstance(item, dict) and item.get("available", True)
        }
        required = {
            "filesystem",
            "editor",
            "shell",
            "computer-control",
            "agent-in-chrome",
            _PLUGIN_NAME,
        }
        if missing := sorted(required - available):
            raise RuntimeError(f"frozen direct catalog is missing {missing}: {catalog}")

        def call(
            server: str,
            tool: str,
            args: dict,
            call_id: str,
            *,
            expect_ok: bool = True,
        ) -> dict:
            return host.request(
                "call",
                expect_ok=expect_ok,
                server=server,
                tool=tool,
                args=args,
                principal=principal,
                call_id=call_id,
                idempotency_key=call_id,
            )

        write_args = {"path": str(target), "content": initial}
        write_id = f"frozen-write-{pass_number}"
        written = call("filesystem", "write_file", write_args, write_id)["result"]
        replay = call("filesystem", "write_file", write_args, write_id)["result"]
        for result in (written, replay):
            if result.get("_meta", {}).get("openagent/location") != "client":
                raise RuntimeError(f"frozen filesystem lost execution location: {result}")
            if result.get("_meta", {}).get("openagent/pathSemantics") != "client-local":
                raise RuntimeError(f"frozen filesystem lost client-local path marker: {result}")
        if written.get("_meta", {}).get("openagent/replayed") is not False:
            raise RuntimeError("first frozen mutation was incorrectly marked replayed")
        if replay.get("_meta", {}).get("openagent/replayed") is not True:
            raise RuntimeError("duplicate frozen mutation did not replay from the durable ledger")
        conflict = call(
            "filesystem",
            "write_file",
            {"path": str(target), "content": "different"},
            write_id,
            expect_ok=False,
        )
        if conflict.get("error", {}).get("code") != "idempotency_conflict":
            raise RuntimeError(f"frozen host accepted a conflicting duplicate: {conflict}")

        call(
            "editor",
            "edit",
            {"file_path": str(target), "old_string": "alpha", "new_string": "beta"},
            f"frozen-edit-{pass_number}",
        )
        read = call(
            "filesystem",
            "read_text_file",
            {"path": str(target)},
            f"frozen-read-{pass_number}",
        )["result"]
        if read.get("content", [{}])[0].get("text") != initial.replace("alpha", "beta"):
            raise RuntimeError(f"frozen file/editor round-trip failed: {read}")

        foreground = call(
            "shell",
            "shell_exec",
            {"command": "echo cli-shell-ok", "timeout": 10_000},
            f"frozen-shell-foreground-{pass_number}",
        )["result"].get("structuredContent") or {}
        if foreground.get("exit_code") != 0 or "cli-shell-ok" not in foreground.get("stdout", ""):
            raise RuntimeError(f"frozen foreground shell failed: {foreground}")

        background_command = (
            "ping -n 2 127.0.0.1 >nul && echo cli-background-ok"
            if os.name == "nt"
            else "sleep 0.2; printf cli-background-ok"
        )
        background = call(
            "shell",
            "shell_exec",
            {"command": background_command, "run_in_background": True},
            f"frozen-shell-background-{pass_number}",
        )["result"].get("structuredContent") or {}
        shell_id = background.get("shell_id")
        if not isinstance(shell_id, str) or not shell_id.startswith("sh_"):
            raise RuntimeError(f"frozen background shell returned no shell_id: {background}")
        output = {}
        for attempt in range(30):
            output = call(
                "shell",
                "shell_output",
                {"shell_id": shell_id, "since_last": False},
                f"frozen-shell-output-{pass_number}-{attempt}",
            )["result"].get("structuredContent") or {}
            if not output.get("still_running"):
                break
            time.sleep(0.1)
        if output.get("exit_code") != 0 or "cli-background-ok" not in output.get(
            "stdout_delta", ""
        ):
            raise RuntimeError(f"frozen background shell completion failed: {output}")
    finally:
        try:
            if consent_enabled and host._process.poll() is None:
                host.request("set_consent", enabled=False, consent_version=1)
        finally:
            host.close()

    if target.read_text(encoding="utf-8") != initial.replace("alpha", "beta"):
        raise RuntimeError("frozen client-local file did not retain the edited contents")
    forbidden_audit_values = (
        b"CLIENT-CONTENT-MUST-NOT-ENTER-AUDIT",
        b"PLUGIN-CONTENT-MUST-NOT-ENTER-AUDIT",
    )
    for audit_path in (home / "host-tools").glob("audit.sqlite3*"):
        audit = audit_path.read_bytes()
        if leaked := next((value for value in forbidden_audit_values if value in audit), None):
            raise RuntimeError(f"local audit leaked {leaked!r}: {audit_path}")


def _run(executable: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), "local-tools", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


if __name__ == "__main__":
    main()
