#!/usr/bin/env python3
"""Release smoke for the real frozen CLI and its packaged capability host."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import platform
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
_CHROME_MARKER = "OPENAGENT_FROZEN_CLI_CHROME_SMOKE"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_WINDOWS_JOB_LAUNCHER = (
    "import subprocess,sys\n"
    "if sys.stdin.buffer.read(1) != b'\\x01': raise SystemExit(125)\n"
    "raise SystemExit(subprocess.call(sys.argv[1:],stdin=subprocess.DEVNULL))\n"
)
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
        # Reuse the exact broker data and browser profile for both passes. A
        # second clean launch is therefore evidence that the first teardown
        # released its singleton, CDP port and every Windows profile handle.
        home = Path(root) / "client-home"
        home.mkdir()
        env = os.environ.copy()
        env["OPENAGENT_HOST_TOOLS_HOME"] = str(home)
        # Release packaging stages the immutable bundle beside the executable.
        # Ignore the CI acquisition path so this smoke proves the distributable
        # layout rather than a build-machine override.
        env.pop("OPENAGENT_HOST_TOOLS_BUNDLE", None)
        env.pop("OPENAGENT_HOST_TOOLS_SIDECAR_DIR", None)
        _configure_plugin(home)
        for pass_number in range(1, _PASS_COUNT + 1):
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
    output = ""
    sidecar_summary = "not-run"
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
        sidecar_summary = asyncio.run(
            _exercise_native_sidecars_through_broker(home, pass_number)
        )
    finally:
        # Revoke consent even when a native sidecar assertion failed. The
        # nested finally still reaps the exact broker child if the frozen CLI
        # cannot process the revocation request.
        try:
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
    return (
        f"Frozen local-tools release smoke pass {pass_number}/{_PASS_COUNT}:\n"
        f"{output.strip()}\nNative broker dispatch: {sidecar_summary}"
    )


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
        self._process: subprocess.Popen | None = None
        self._windows_job: _WindowsJob | None = None

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("frozen broker was started twice")
        command = [str(self._executable), "--broker"]
        if os.name == "nt":
            # A PyInstaller one-file executable is a bootloader plus its real
            # child process. Launch it below a gated Python root which is put in
            # a kill-on-close Job Object *before* the frozen broker may start.
            # Node and detached Chromium descendants then remain owned even if
            # an intermediate process exits and Windows reparents them.
            self._process, self._windows_job = _spawn_windows_job_process(
                command,
                env=self._env,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr,
                close_fds=True,
            )
        else:
            self._process = subprocess.Popen(
                command,
                env=self._env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr,
                text=True,
                close_fds=True,
            )
        try:
            asyncio.run(_wait_for_broker(self._home, self._process))
        except Exception as exc:
            stderr = self.stderr_text().strip()
            self.close()
            if stderr:
                raise RuntimeError(
                    f"frozen broker failed readiness: {exc}; stderr:\n{stderr}"
                ) from exc
            raise

    def close(self) -> None:
        process = self._process
        self._process = None
        windows_job = self._windows_job
        self._windows_job = None
        close_error: BaseException | None = None
        try:
            if windows_job is not None:
                # TerminateJobObject is scoped to the exact gated tree created
                # in start(). It waits on an explicit active-process count, so
                # the broker, Node sidecar and detached Chromium are all gone
                # before profile/socket assertions run.
                windows_job.terminate(timeout=15)
                if process is not None:
                    process.wait(timeout=5)
            elif process is not None and process.poll() is None:
                # Consent revocation has already closed every plugin/sidecar.
                # This signal is therefore scoped to the exact broker child
                # owned by this smoke; it cannot affect a user's shared host.
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait(timeout=5)
                    close_error = RuntimeError(
                        "frozen broker ignored termination and required exact-child cleanup"
                    )
                    close_error.__cause__ = exc
            asyncio.run(_require_broker_unavailable(self._home))
            _require_windows_profiles_unlocked(self._home)
        except BaseException as exc:
            if close_error is None:
                close_error = exc
        finally:
            if windows_job is not None:
                try:
                    windows_job.close()
                except BaseException as exc:
                    if close_error is None:
                        close_error = exc
            self._stderr.close()
        if close_error is not None:
            raise close_error

    def stderr_text(self) -> str:
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()


class _WindowsJob:
    """Own one Windows process tree, including detached descendants."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, process: subprocess.Popen):
        if os.name != "nt":  # pragma: no cover - protected by the caller
            raise RuntimeError("Windows Job Objects are only available on Windows")

        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        abi_sizes = (
            ctypes.sizeof(BasicLimitInformation),
            ctypes.sizeof(ExtendedLimitInformation),
            ctypes.sizeof(BasicAccountingInformation),
        )
        if ctypes.sizeof(ctypes.c_void_p) != 8 or abi_sizes != (64, 144, 48):
            raise RuntimeError(f"unsupported Windows Job Object ABI: {abi_sizes}")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._accounting_type = BasicAccountingInformation
        self._handle = handle
        try:
            limits = ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = (
                self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not kernel32.SetInformationJobObject(
                handle,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = wintypes.HANDLE(int(process._handle))
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            kernel32.CloseHandle(handle)
            self._handle = None
            raise

    def terminate(self, *, timeout: float) -> None:
        handle = self._handle
        if handle is None:
            return
        if not self._kernel32.TerminateJobObject(handle, 1):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while True:
            accounting = self._accounting_type()
            if not self._kernel32.QueryInformationJobObject(
                handle,
                self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                self._ctypes.byref(accounting),
                self._ctypes.sizeof(accounting),
                None,
            ):
                raise self._ctypes.WinError(self._ctypes.get_last_error())
            if accounting.ActiveProcesses == 0:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Windows broker Job Object retained {accounting.ActiveProcesses} processes"
                )
            # This is bounded polling of the kernel's active-process count, not
            # a grace-period sleep which could silently leave children alive.
            time.sleep(0.01)

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None and not self._kernel32.CloseHandle(handle):
            raise self._ctypes.WinError(self._ctypes.get_last_error())


def _spawn_windows_job_process(
    command: list[str],
    **popen_kwargs,
) -> tuple[subprocess.Popen, _WindowsJob]:
    """Gate child creation until its launcher belongs to a Job Object."""

    if os.name != "nt":  # pragma: no cover - exercised by Windows CI
        raise RuntimeError("Windows Job Object launch requested on a non-Windows host")
    process = subprocess.Popen(
        [sys.executable, "-c", _WINDOWS_JOB_LAUNCHER, *command],
        stdin=subprocess.PIPE,
        **popen_kwargs,
    )
    job: _WindowsJob | None = None
    try:
        job = _WindowsJob(process)
        assert process.stdin is not None
        process.stdin.write(b"\x01")
        process.stdin.flush()
        process.stdin.close()
        return process, job
    except BaseException:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if job is not None:
            try:
                job.close()
            except Exception:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        raise


def _require_windows_profiles_unlocked(home: Path) -> None:
    """Prove Chromium released the exact profile files used by the smoke."""

    if os.name != "nt":
        return
    principals = home / "host-tools" / "agent-in-chrome" / "principals"
    for profile in principals.glob("*/profile"):
        candidates = list(profile.rglob("journal.baj"))
        candidates.extend(
            path
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket")
            if (path := profile / name).exists()
        )
        # A minimal/brand-new profile may not contain the known Chromium lock
        # files yet; renaming the profile itself still probes directory handles.
        if not candidates:
            candidates.append(profile)
        for path in candidates:
            probe = path.with_name(f"{path.name}.openagent-release-unlocked")
            if probe.exists():
                raise RuntimeError(f"stale profile unlock probe exists: {probe}")
            try:
                path.replace(probe)
                probe.replace(path)
            except OSError as exc:
                if probe.exists() and not path.exists():
                    try:
                        probe.replace(path)
                    except OSError:
                        pass
                raise RuntimeError(f"Chromium profile remains locked: {path}") from exc


async def _wait_for_broker(
    home: Path,
    process: subprocess.Popen,
    *,
    timeout: float = 60.0,
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


async def _broker_request(
    client,
    request_id: str,
    request_type: str,
    *,
    timeout: float = 30.0,
    **fields,
) -> dict:
    await client.send({"id": request_id, "type": request_type, **fields})
    while True:
        response = await asyncio.wait_for(client.receive(), timeout=timeout)
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


async def _exercise_native_sidecars_through_broker(
    home: Path,
    pass_number: int,
) -> str:
    """Dispatch both native sidecars through the real frozen broker.

    The browser principal mirrors the trusted fields supplied by the Gateway.
    Its network/account pair is stable across both passes in this temporary
    home, so pass two must reopen the exact profile closed by pass one.
    ``release_principal`` is awaited before the broker is disabled; the owned
    Windows Job Object remains the final fail-closed teardown boundary.
    """

    from openagent_host_tools import HostPaths
    from openagent_host_tools.local_broker import LocalBrokerClient

    principal = {
        "kind": "client",
        "client_instance_id": f"frozen-cli-smoke-{pass_number}",
        "device_label": "Frozen CLI release smoke",
        "device_id": "frozen-cli-device",
        "account_id": "frozen-cli-account",
        "network_id": "frozen-cli-network",
        "generation": pass_number,
    }
    client = LocalBrokerClient(HostPaths.discover(home))
    await client.connect()
    primary_error: BaseException | None = None
    try:
        computer = await _exercise_computer_control(client, principal, pass_number)
        chrome = await _exercise_agent_in_chrome(client, principal, pass_number)
        return f"computer-control({computer}); agent-in-chrome({chrome})"
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        # A large or malformed frame can poison this StreamReader. Close it
        # first, then use a fresh broker connection for explicit principal
        # release; the server also releases the principal on disconnect.
        await client.close()
        cleanup_client = LocalBrokerClient(HostPaths.discover(home))
        try:
            await cleanup_client.connect()
            released = await _broker_request(
                cleanup_client,
                f"sidecar-release-{pass_number}",
                "release_principal",
                timeout=60,
                principal=principal,
            )
            if released.get("ok") is not True or not (
                released.get("result") or {}
            ).get("released"):
                raise RuntimeError(
                    f"frozen broker did not release its native sidecars: {released}"
                )
        except BaseException:
            if primary_error is None:
                raise
        finally:
            await cleanup_client.close()


async def _broker_tool_call(
    client,
    *,
    principal: dict,
    server: str,
    tool: str,
    args: dict,
    call_id: str,
    timeout: float = 90.0,
) -> dict:
    response = await _broker_request(
        client,
        f"request-{call_id}",
        "call",
        timeout=timeout,
        server=server,
        tool=tool,
        args=args,
        principal=principal,
        call_id=call_id,
        idempotency_key=call_id,
    )
    if response.get("ok") is not True:
        raise RuntimeError(f"frozen {server}.{tool} broker dispatch failed: {response}")
    result = response.get("result") or {}
    if result.get("_meta", {}).get("openagent/location") != "client":
        raise RuntimeError(f"frozen {server}.{tool} lost client location: {result}")
    return result


async def _exercise_computer_control(
    client,
    principal: dict,
    pass_number: int,
) -> str:
    cursor = await _broker_tool_call(
        client,
        principal=principal,
        server="computer-control",
        tool="computer",
        args={"action": "get_cursor_position"},
        call_id=f"frozen-computer-cursor-{pass_number}",
    )
    cursor_outcome = _validate_computer_result(cursor, "get_cursor_position")
    if cursor_outcome != "granted":
        # Accessibility/display is a prerequisite for the screenshot because
        # computer-control draws the cursor crosshair into the captured image.
        # Do not hammer a denied native helper with a redundant second probe;
        # the exact OS denial above already proves real broker dispatch.
        return f"cursor={cursor_outcome}, screenshot=skipped-after-os-denial"
    screenshot = await _broker_tool_call(
        client,
        principal=principal,
        server="computer-control",
        tool="computer",
        args={"action": "get_screenshot"},
        call_id=f"frozen-computer-screenshot-{pass_number}",
    )
    screenshot_outcome = _validate_computer_result(screenshot, "get_screenshot")
    return f"cursor={cursor_outcome}, screenshot={screenshot_outcome}"


def _validate_computer_result(result: dict, action: str) -> str:
    if result.get("isError") is True:
        text = _tool_result_text(result).lower()
        markers = _allowed_computer_denials(action)
        if not markers or not any(marker in text for marker in markers):
            raise RuntimeError(
                f"unexpected computer-control {action} denial on {sys.platform}: "
                f"{_tool_result_text(result) or result!r}"
            )
        return "expected-os-denial"

    if action == "get_cursor_position":
        for item in result.get("content") or []:
            if item.get("type") != "text":
                continue
            try:
                value = json.loads(str(item.get("text") or ""))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and all(
                isinstance(value.get(axis), int) and not isinstance(value.get(axis), bool)
                for axis in ("x", "y")
            ):
                return "granted"
        raise RuntimeError(f"computer-control returned invalid cursor coordinates: {result}")

    for item in result.get("content") or []:
        if item.get("type") != "image" or item.get("mimeType") != "image/png":
            continue
        try:
            payload = base64.b64decode(item.get("data") or "", validate=True)
        except (TypeError, ValueError, binascii.Error) as exc:
            raise RuntimeError("computer-control returned invalid base64 PNG data") from exc
        if not payload.startswith(_PNG_SIGNATURE) or not payload.endswith(_PNG_IEND):
            raise RuntimeError("computer-control returned an invalid or truncated PNG")
        return "granted"
    raise RuntimeError(f"computer-control returned no PNG screenshot: {result}")


def _allowed_computer_denials(action: str) -> tuple[str, ...]:
    if sys.platform == "darwin":
        if action == "get_cursor_position":
            return ("macos accessibility permission required",)
        return (
            "macos accessibility permission required",
            "macos screen recording permission required",
        )
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        # Headless source/test jobs are allowed to prove the native dispatch
        # boundary by reaching one of these stable display failures. Release
        # jobs with Xvfb/Wayland are not: when a display is advertised, both
        # calls must produce real coordinates/pixels.
        return (
            "enigo init failed",
            "xopendisplay",
            "failed to establish x11 connection",
            "xcap::monitor::all failed",
            "no monitors detected",
            "wayland connection",
        )
    # Windows release runners are interactive desktops. A denial there, or on
    # Linux with an advertised display, is a release failure rather than an
    # accepted environment limitation.
    return ()


def _tool_result_text(result: dict) -> str:
    return "\n".join(
        str(item.get("text") or "")
        for item in result.get("content") or []
        if isinstance(item, dict) and item.get("type") == "text"
    )


async def _chrome_smoke_page(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        body = (
            "<!doctype html><meta charset=utf-8><title>OpenAgent CLI smoke</title>"
            f'<main id="marker">{_CHROME_MARKER}</main>'
        ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


async def _exercise_agent_in_chrome(
    client,
    principal: dict,
    pass_number: int,
) -> str:
    page_server = await asyncio.start_server(_chrome_smoke_page, "127.0.0.1", 0)
    assert page_server.sockets
    page_port = int(page_server.sockets[0].getsockname()[1])
    try:
        context = await _broker_tool_call(
            client,
            principal=principal,
            server="agent-in-chrome",
            tool="tabs_context_mcp",
            args={"createIfEmpty": True},
            call_id=f"frozen-chrome-context-{pass_number}",
        )
        if _is_expected_missing_chrome(context):
            return "expected-linux-arm64-prerequisite"
        _require_tool_success(context, "agent-in-chrome tab discovery")
        try:
            first_line = _tool_result_text(context).splitlines()[0]
            tab_id = json.loads(first_line)["availableTabs"][0]["tabId"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"frozen agent-in-chrome returned invalid tab context: {context}"
            ) from exc
        if not isinstance(tab_id, int) or isinstance(tab_id, bool):
            raise RuntimeError(f"frozen agent-in-chrome returned invalid tab id: {tab_id!r}")

        navigated = await _broker_tool_call(
            client,
            principal=principal,
            server="agent-in-chrome",
            tool="navigate",
            args={"url": f"http://127.0.0.1:{page_port}/smoke", "tabId": tab_id},
            call_id=f"frozen-chrome-navigate-{pass_number}",
        )
        _require_tool_success(navigated, "agent-in-chrome navigation")
        page = await _broker_tool_call(
            client,
            principal=principal,
            server="agent-in-chrome",
            tool="get_page_text",
            args={"tabId": tab_id},
            call_id=f"frozen-chrome-read-{pass_number}",
        )
        _require_tool_success(page, "agent-in-chrome page read")
        if _CHROME_MARKER not in _tool_result_text(page):
            raise RuntimeError(
                "frozen agent-in-chrome did not read the deterministic local page: "
                f"{_tool_result_text(page)!r}"
            )
        return "navigate+read=granted"
    finally:
        page_server.close()
        await page_server.wait_closed()


def _is_expected_missing_chrome(result: dict) -> bool:
    """Accept one release-runner prerequisite gap without hiding regressions.

    Only GitHub's Linux ARM64 matrix entry opts in. Even there, the real
    agent-in-chrome sidecar must have launched and returned its stable
    "tried: none" error; a found-but-broken browser or any other failure stays
    release-blocking. Other architectures remain strict so losing Chrome from
    a previously capable runner fails loudly.
    """

    if os.environ.get("OPENAGENT_RELEASE_ALLOW_MISSING_CHROME") != "1":
        return False
    if not sys.platform.startswith("linux"):
        return False
    if platform.machine().lower() not in {"arm64", "aarch64"}:
        return False
    if result.get("isError") is not True:
        return False
    text = _tool_result_text(result).lower()
    return all(
        marker in text
        for marker in (
            "could not launch any chromium-based browser",
            "tried: none",
            "set openagent_chrome_binary",
        )
    )


def _require_tool_success(result: dict, label: str) -> None:
    if result.get("isError") is True:
        raise RuntimeError(f"frozen {label} failed: {_tool_result_text(result) or result!r}")


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
