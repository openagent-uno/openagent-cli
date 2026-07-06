"""OpenAgent CLI — interactive client for any OpenAgent Gateway.

Usage:
    openagent-cli connect localhost:8765
    openagent-cli connect localhost:8765 --token mysecret
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

# Deferred import: src.client pulls in openagent.gateway.protocol which
# (in the installed server package) does ``from src.gateway.commands import
# COMMANDS`` at module level — a ``from src.*`` import that resolves to the
# CLI's own src/ rather than the server's src/ and therefore blows up.
# With ``from __future__ import annotations`` already active, every
# GatewayClient annotation in this file is a lazy string, so no NameError
# at definition time.  The three actual call sites below import lazily.
# TERMINAL_* constants used in _run_terminal are also imported there.
if False:  # TYPE_CHECKING placeholder — satisfies IDEs without importing
    from src.client import GatewayClient, TERMINAL_OUTPUT, TERMINAL_EXIT, TERMINAL_ERROR  # noqa: F401

console = Console()


# ── Tool-status formatter (inlined to avoid coupling to openagent package) ──

def format_tool_status(raw: str) -> str:
    """Convert a status string (possibly JSON tool event) into a human line."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or "tool" not in data:
            return raw
    except (json.JSONDecodeError, TypeError):
        return raw
    tool = data["tool"]
    status = data.get("status", "running")
    if status == "running":
        return f"Using {tool}..."
    if status == "error":
        return f"✗ {tool} failed: {data.get('error', 'unknown error')}"
    return f"✓ {tool} done"


# ── Helpers ──────────────────────────────────────────────────────────────

async def _render_response(response: dict, client: "GatewayClient | None" = None) -> None:
    resp_text = response.get("text", "")
    if response.get("type") == "error":
        console.print(f"[red]Error: {resp_text}[/red]")
    else:
        console.print(Markdown(resp_text))
        model = response.get("model")
        if model:
            console.print(f"[dim]Model: {model}[/dim]")

    # Render attachments the agent attached to the response (via
    # ``[IMAGE:/path]`` / ``[FILE:/path]`` / ``[VOICE:/path]`` /
    # ``[VIDEO:/path]`` markers that the gateway stripped from the
    # text and moved to a side-channel). The gateway gives us the
    # absolute path as it exists on its own filesystem; locally
    # colocated CLIs can read that path verbatim, remote CLIs need to
    # fetch via ``/api/files``. We try local read first (zero copy,
    # works for single-machine dev installs), then fall back to the
    # HTTP download.
    attachments = response.get("attachments") or []
    if attachments and response.get("type") != "error":
        console.print("[dim]Attachments:[/dim]")
        for att in attachments:
            remote_path = att.get("path", "")
            filename = att.get("filename") or os.path.basename(remote_path) or "attachment"
            kind = att.get("type", "file")
            icon = {"image": "🖼", "voice": "🎤", "video": "🎬", "file": "📄"}.get(kind, "📎")

            if remote_path and os.path.isfile(remote_path):
                console.print(f"  {icon} [cyan]{filename}[/cyan] [dim]→ {remote_path}[/dim]")
                continue

            # Not reachable locally — download into cwd via /api/files.
            if client is None:
                console.print(f"  {icon} [yellow]{filename}[/yellow] [dim](remote: {remote_path}; no client bound to fetch)[/dim]")
                continue
            dest = Path.cwd() / filename
            # Avoid clobbering an existing file with the same name by
            # suffixing a counter — the agent may emit many files with
            # generic names (report.pdf, screenshot.png, …) across a
            # session.
            if dest.exists():
                stem = dest.stem
                suffix = dest.suffix
                for i in range(1, 1000):
                    candidate = dest.with_name(f"{stem}-{i}{suffix}")
                    if not candidate.exists():
                        dest = candidate
                        break
            try:
                bytes_written = await client.download_file(remote_path, str(dest))
                console.print(f"  {icon} [green]{filename}[/green] [dim]→ {dest} ({bytes_written:,} bytes)[/dim]")
            except Exception as e:  # noqa: BLE001 — inform user of any fetch failure, keep loop alive
                console.print(f"  {icon} [red]{filename}[/red] [dim](download failed: {e})[/dim]")
    console.print()


async def _send_message_with_indicator(
    client: "GatewayClient", text: str, session_id: str
) -> None:
    """Send a chat turn while showing an animated "Reasoning…" spinner,
    then render the response.

    The spinner is driven by the server's transient ``reasoning`` frames
    (via ``on_reasoning``): it animates while the agent is thinking with
    no visible output yet, and collapses the moment output starts or the
    turn ends. Tool steps (``on_status``) are printed as persistent lines
    above the spinner so the step trail survives the turn. The spinner is
    rendered in a ``transient`` ``Live`` region so it is fully erased — and
    the prompt left intact — before the response prints, even on error.
    """
    import signal as _signal

    spinner = Spinner("dots", text="[dim]Reasoning…[/dim]")
    idle = Text("")  # collapses the Live region (active=false / between steps)

    loop = asyncio.get_running_loop()
    interrupted = {"v": False}
    response = None

    # ``Live`` auto-refreshes on its own thread, so the spinner animates
    # while ``send_message`` is awaited and the WS callbacks fire on the
    # event loop — neither blocks the other.
    with Live(idle, console=console, refresh_per_second=12, transient=True) as live:
        async def on_reasoning(active: bool) -> None:
            live.update(spinner if active else idle)

        async def on_status(status: str) -> None:
            # Tool-status text is untrusted (tool names, error strings, paths
            # with brackets) — feed it as a styled Text, never as a markup
            # string, so a stray ``[/x]`` can't raise MarkupError or silently
            # drop the rest of the line.
            live.console.print(Text(format_tool_status(status), style="dim"))

        # Drive the turn as a task so a Ctrl-C mid-turn becomes a
        # server-side barge-in (stop) instead of tearing the CLI down. The
        # blocking REPL can't read a typed ``/stop`` while a turn streams,
        # so Ctrl-C is the only interactive stop the CLI has — make it count.
        send_task = asyncio.ensure_future(
            client.send_message(
                text, session_id, on_status=on_status, on_reasoning=on_reasoning,
            )
        )

        def _on_sigint() -> None:
            # First Ctrl-C: ask the server to cancel THIS turn (interrupt
            # frame → _cancel_active_turn). The turn's terminal frame then
            # resolves ``send_message`` normally and we drop back to the
            # prompt. The handler is removed in the finally so a Ctrl-C at
            # the idle prompt still quits the app.
            if interrupted["v"]:
                return
            interrupted["v"] = True
            loop.create_task(client.send_interrupt(session_id, reason="manual"))

        # POSIX: install a loop SIGINT handler for the turn's lifetime
        # (same pattern the terminal bridge uses for SIGWINCH). Windows /
        # loops without signal support fall back to the KeyboardInterrupt
        # except below.
        sigint_installed = False
        if sys.platform != "win32":
            try:
                loop.add_signal_handler(_signal.SIGINT, _on_sigint)
                sigint_installed = True
            except (NotImplementedError, RuntimeError, ValueError):
                sigint_installed = False

        try:
            response = await send_task
        except KeyboardInterrupt:
            # Fallback stop path (no loop signal handler): send the
            # barge-in, then drain the now-cancelled turn so we stay in
            # the REPL instead of unwinding out of asyncio.run.
            interrupted["v"] = True
            try:
                await asyncio.shield(
                    client.send_interrupt(session_id, reason="manual")
                )
                response = await send_task
            except Exception:
                response = None
        finally:
            live.update(idle)
            if sigint_installed:
                try:
                    loop.remove_signal_handler(_signal.SIGINT)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass

    if interrupted["v"]:
        console.print("[dim]⏹  Interrupted.[/dim]")
    if response is not None:
        await _render_response(response, client=client)


def _open_in_editor(initial_text: str, suffix: str = ".md") -> str | None:
    """Open initial_text in $EDITOR (or vi/notepad), return edited content or None on cancel."""
    editor = os.environ.get("EDITOR") or ("notepad" if sys.platform == "win32" else "vi")
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8") as f:
        f.write(initial_text)
        tmp_path = f.name
    try:
        proc = subprocess.run([editor, tmp_path])
        if proc.returncode != 0:
            return None
        with open(tmp_path, encoding="utf-8") as f:
            new_text = f.read()
        # Treat "no change" as cancel for safety
        if new_text == initial_text:
            console.print("[dim]No changes.[/dim]")
            return None
        return new_text
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _print_help() -> None:
    table = Table(title="OpenAgent CLI Commands", show_header=False)
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Description")
    rows = [
        ("/help", "Show this help"),
        ("/new", "Start a fresh conversation"),
        ("/sessions, /switch <id>", "List or switch sessions"),
        ("/rename <id> <name>", "Rename a session"),
        ("/delete <id>", "Delete a session (with confirmation)"),
        ("/file <path> [more...]", "Attach one or more files/images to the agent"),
        ("/stop", "Cancel the current operation"),
        ("/status", "Show agent status & queue"),
        ("/clear", "Clear the message queue"),
        ("/usage", "Show monthly spend & budget"),
        ("/vault", "Browse, search, edit notes"),
        ("/mcps", "List & toggle MCP servers"),
        ("/terminal, /shell", "Open an interactive terminal on the agent host (SSH-like)"),
        ("/compact", "Summarize & compress conversation history to free context"),
        ("/model [id]", "Show or switch the model for this session (/model default to unpin)"),
        ("/models", "List providers & switch active model"),  # local interactive menu
        ("/providers", "List, test, add providers"),
        ("/settings", "Edit identity, prompt, channels, dream, auto-update"),
        ("/tasks", "Manage scheduled tasks + view run history"),
        ("/workflows", "List workflows, run, view run history, set concurrency"),
        ("/config", "Show config summary"),
        ("/restart", "Restart the agent"),
        ("/update", "Check for and install updates"),
        ("/quit, /exit, /q", "Disconnect & exit"),
    ]
    for cmd, desc in rows:
        table.add_row(cmd, desc)
    console.print(table)


# ── CLI entry ────────────────────────────────────────────────────────────

@click.group()
def cli():
    """OpenAgent CLI — connect to any OpenAgent Gateway."""
    pass


@cli.command()
@click.argument("target")
@click.option("--password", default=None, help="Password (omit to be prompted securely)")
@click.option("--handle", "handle_override", default=None,
              help="When redeeming a user-role ticket, the handle to register as.")
@click.option("--agent", "agent_handle", default=None,
              help="Specific agent handle to connect to (default: last used or first available)")
def connect(target: str, password: str | None, handle_override: str | None,
            agent_handle: str | None):
    """Connect to an OpenAgent network and start interactive session.

    \b
    Two forms:
      openagent-cli connect oa1abcdef…       # invite ticket — first time / new device
      openagent-cli connect alice@homelab    # existing membership — just need password
    """
    asyncio.run(_run_connect(
        target=target,
        password=password,
        handle_override=handle_override,
        target_agent_handle=agent_handle,
    ))


async def _run_connect(
    *,
    target: str,
    password: str | None,
    handle_override: str | None,
    target_agent_handle: str | None,
):
    from openagent.network.cli_commands import parse_handle_at_network
    from openagent.network import user_store
    from openagent.network.client.login import (
        LoginError,
        register as net_register,
        login as net_login,
    )
    from openagent.network.identity import load_or_create_identity
    from openagent.network.iroh_node import IrohNode
    from openagent.network.ticket import InviteTicket, TicketError, looks_like_ticket
    import getpass

    # Resolve target → (handle, network_name, coordinator_node_id?, invite_code?, ticket_role)
    coordinator_node_id: str | None = None
    invite_code: str | None = None
    ticket_role: str | None = None
    bind_to: str = ""
    network_name: str
    handle: str
    ticket: InviteTicket | None = None

    if looks_like_ticket(target):
        try:
            ticket = InviteTicket.decode(target)
        except TicketError as e:
            console.print(f"[red]Invalid ticket:[/red] {e}")
            return
        coordinator_node_id = ticket.coordinator_node_id
        invite_code = ticket.code
        ticket_role = ticket.role
        bind_to = ticket.bind_to
        network_name = ticket.network_name
        # ``role=device`` tickets are bound to a handle; user-role
        # tickets let the new user pick one. We accept ``--handle`` to
        # bypass the prompt entirely (useful for scripted setups).
        if bind_to:
            handle = bind_to
        elif handle_override:
            handle = handle_override.strip().lower()
        else:
            handle = Prompt.ask(
                f"[bold]Choose a handle for {network_name}[/bold]",
            ).strip().lower()
            if not handle:
                console.print("[red]Handle required.[/red]")
                return
    else:
        try:
            handle, network_name = parse_handle_at_network(target)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            console.print("[dim]Tip: paste an invite ticket (oa1…) for first-time connects.[/dim]")
            return

    store = user_store.load()
    existing = user_store.find(store, network_name, handle)
    user_store.ensure_user_identity_dir()

    if password is None:
        password = getpass.getpass(f"Password for {handle}@{network_name}: ")
    if not password:
        console.print("[red]Password is required.[/red]")
        return

    from openagent.network.peers import coordinator_node_id_to_pubkey_bytes

    device_identity = load_or_create_identity(user_store.user_identity_path())
    node = IrohNode(device_identity)
    await node.start()

    try:
        if existing is None:
            # First-time onboarding for this network. The ticket carries
            # everything we need (coordinator NodeId, network name + ID,
            # invite code, role). The legacy --coordinator/--invite flags
            # are gone — paste a ticket instead.
            if coordinator_node_id is None or invite_code is None:
                console.print(
                    "[red]This network isn't in your user store.[/red] "
                    "Paste an invite ticket (starts with [cyan]oa1[/cyan]) instead "
                    "of [cyan]handle@network[/cyan] for first-time connects.",
                )
                return
            coord_pubkey = coordinator_node_id_to_pubkey_bytes(coordinator_node_id)
            # First-contact addressing hints from the ticket. The
            # coordinator's NodeId may not yet be in our local iroh
            # discovery cache (just-restarted pod, post-DMG-install
            # mDNS permission gate, etc.); passing the relay + direct
            # addresses lets the dial skip discovery entirely.
            ticket_relay = getattr(ticket, "relay_url", None) if ticket else None
            ticket_addrs = list(getattr(ticket, "addresses", None) or []) if ticket else []
            try:
                if ticket_role == "device":
                    # Existing account, new device pairing — just login,
                    # the coordinator binds the device key on success.
                    cert_wire = await net_login(
                        node=node,
                        coordinator_node_id=coordinator_node_id,
                        coordinator_pubkey_bytes=coord_pubkey,
                        handle=handle,
                        password=password,
                        device_identity=device_identity,
                        network_id="",  # learned from cert
                        invite_code=invite_code,
                        relay_url=ticket_relay,
                        addresses=ticket_addrs,
                    )
                else:
                    cert_wire = await net_register(
                        node=node,
                        coordinator_node_id=coordinator_node_id,
                        coordinator_pubkey_bytes=coord_pubkey,
                        handle=handle,
                        password=password,
                        invite_code=invite_code,
                        device_identity=device_identity,
                        network_id="",  # learned from cert; we don't ship it on the wire
                        relay_url=ticket_relay,
                        addresses=ticket_addrs,
                    )
            except LoginError as e:
                console.print(f"[red]Login failed:[/red] {e}")
                return
            # The cert tells us the canonical network_id — verify and store.
            from openagent.network.auth.device_cert import verify_cert
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            cert = verify_cert(
                cert_wire,
                coordinator_pubkey=Ed25519PublicKey.from_public_bytes(coord_pubkey),
            )
            stored = user_store.add_or_update(
                store,
                name=network_name,
                network_id=cert.network_id,
                coordinator_node_id=coordinator_node_id,
                coordinator_pubkey_hex=coord_pubkey.hex(),
                handle=handle,
                coordinator_relay_url=ticket_relay,
                coordinator_addresses=ticket_addrs,
            )
            user_store.write_cert(stored, cert_wire)
            user_store.save(store)
            console.print(f"[green]Joined {network_name!r} as {handle!r}.[/green]")
        else:
            # Existing membership — just refresh the cert. Prefer any
            # fresh address hints from the pasted ticket; fall back to
            # the addresses we cached the last time we joined.
            ticket_relay = getattr(ticket, "relay_url", None) if ticket else None
            ticket_addrs = list(getattr(ticket, "addresses", None) or []) if ticket else []
            relay_hint = ticket_relay or existing.coordinator_relay_url
            addrs_hint = ticket_addrs or existing.coordinator_addresses
            try:
                cert_wire = await net_login(
                    node=node,
                    coordinator_node_id=existing.coordinator_node_id,
                    coordinator_pubkey_bytes=existing.coordinator_pubkey_bytes,
                    handle=handle,
                    password=password,
                    device_identity=device_identity,
                    network_id=existing.network_id,
                    relay_url=relay_hint,
                    addresses=addrs_hint,
                )
            except LoginError as e:
                console.print(f"[red]Login failed:[/red] {e}")
                return
            user_store.write_cert(existing, cert_wire)
            # If the user pasted a fresh ticket, persist the new
            # hints — coordinators rotate ports across restarts and the
            # old cached addresses can go stale.
            if ticket_relay or ticket_addrs:
                user_store.add_or_update(
                    store,
                    name=existing.name,
                    network_id=existing.network_id,
                    coordinator_node_id=existing.coordinator_node_id,
                    coordinator_pubkey_hex=existing.coordinator_pubkey_hex,
                    handle=existing.handle,
                    coordinator_relay_url=ticket_relay,
                    coordinator_addresses=ticket_addrs,
                )
            user_store.save(store)
            stored = existing

        # We have a fresh cert. Re-derive the binding + dialer + agent
        # list, then hand off to the interactive REPL via from_network.
        await node.stop()
    except Exception:
        await node.stop()
        raise

    try:
        from src.client import GatewayClient  # lazy: avoids module-level import of openagent.gateway
        client = await GatewayClient.from_network(
            handle=handle,
            network_name=network_name,
            password=password,
            target_agent_handle=target_agent_handle,
        )
    except Exception as e:
        console.print(f"[red]Could not open gateway connection:[/red] {e}")
        return

    try:
        await client.connect()
    except Exception as e:
        console.print(f"[red]Connection failed:[/red] {e}")
        await client.disconnect()
        return

    # Persist the chosen agent so subsequent connects pick the same one.
    if client.target_handle:
        store2 = user_store.load()
        store2.active_network = network_name
        store2.active_agent = client.target_handle
        user_store.save(store2)

    await _interactive_loop(client, network_name=network_name, handle=handle)


@cli.command("logout")
@click.argument("network_name", required=False, default=None)
def logout_cmd(network_name: str | None):
    """Drop a network membership locally (cert + entry in the user store)."""
    from openagent.network import user_store

    store = user_store.load()
    if network_name is None:
        if not store.networks:
            console.print("[yellow]No networks to log out from.[/yellow]")
            return
        for n in store.networks:
            console.print(f"  - [cyan]{n.handle}@{n.name}[/cyan]")
        console.print("[dim]Pass the network name as an argument to remove it.[/dim]")
        return
    ok = user_store.remove(store, network_name)
    if ok:
        user_store.save(store)
        console.print(f"[green]Logged out of {network_name!r}.[/green]")
    else:
        console.print(f"[yellow]No such network: {network_name}[/yellow]")


@cli.command("networks")
def networks_cmd():
    """List networks this device has joined."""
    from openagent.network import user_store

    store = user_store.load()
    if not store.networks:
        console.print("[dim]No networks joined.[/dim]")
        return
    table = Table(title="Joined networks")
    table.add_column("Name", style="cyan")
    table.add_column("Handle")
    table.add_column("Coordinator NodeId", style="dim")
    table.add_column("Active", justify="center")
    for n in store.networks:
        active = "✓" if store.active_network == n.name else ""
        table.add_row(n.name, n.handle, n.coordinator_node_id[:24] + "…", active)
    console.print(table)


@cli.command("agents")
@click.option("--network", "network_name", default=None,
              help="Which network to list (default: active)")
def agents_cmd(network_name: str | None):
    """List agents in a network (queries the coordinator)."""
    asyncio.run(_run_agents_cli(network_name))


async def _run_agents_cli(network_name: str | None):
    from openagent.network import user_store
    from openagent.network.client.login import list_agents as coord_list_agents
    from openagent.network.identity import load_or_create_identity
    from openagent.network.iroh_node import IrohNode

    store = user_store.load()
    if network_name is None:
        network_name = store.active_network
    if network_name is None:
        console.print("[red]No active network. Run `openagent-cli connect <handle>@<network>` first.[/red]")
        return
    net = user_store.find(store, network_name)
    if net is None:
        console.print(f"[red]Unknown network: {network_name}[/red]")
        return
    user_store.ensure_user_identity_dir()
    device_identity = load_or_create_identity(user_store.user_identity_path())
    node = IrohNode(device_identity)
    await node.start()
    try:
        agents = await coord_list_agents(node=node, coordinator_node_id=net.coordinator_node_id)
    finally:
        await node.stop()
    if not agents:
        console.print("[dim]No agents registered.[/dim]")
        return
    table = Table(title=f"Agents in {network_name}")
    table.add_column("Handle", style="cyan")
    table.add_column("NodeId", style="dim")
    table.add_column("Owner", style="dim")
    table.add_column("Label")
    table.add_column("Active", justify="center")
    for a in agents:
        active = "✓" if store.active_agent == a.get("handle") else ""
        table.add_row(
            a.get("handle", ""), a.get("node_id", "")[:24] + "…",
            a.get("owner_handle", ""), a.get("label") or "", active,
        )
    console.print(table)


@cli.command("use")
@click.argument("agent_handle")
def use_cmd(agent_handle: str):
    """Set the default agent the next ``connect`` will pick."""
    from openagent.network import user_store

    store = user_store.load()
    store.active_agent = agent_handle
    user_store.save(store)
    console.print(f"[green]Active agent set to {agent_handle!r}.[/green]")


# ── /api/network/* — members & invitations via the gateway ─────────────


async def _open_gateway_for_rest(network_name: str | None, password: str | None):
    """Spin up an authed GatewayClient for one-shot REST calls.

    Loads the active network from the user store, refreshes the
    device cert if needed (prompting for the password when the
    cached cert can't be reused), opens the loopback proxy + WS,
    and returns the live client. Caller is responsible for closing.
    """
    import getpass
    from openagent.network import user_store

    store = user_store.load()
    if network_name is None:
        network_name = store.active_network
    if network_name is None:
        console.print(
            "[red]No active network.[/red] Run "
            "[cyan]openagent-cli connect <handle>@<network>[/cyan] first."
        )
        return None, None

    net = user_store.find(store, network_name)
    if net is None:
        console.print(f"[red]Unknown network: {network_name}[/red]")
        return None, None

    # The cert may be valid (no password needed) or expired (prompt).
    # GatewayClient.from_network handles both branches — we just need
    # to surface the prompt before it raises PermissionError.
    from src.client import GatewayClient  # lazy: avoids module-level import of openagent.gateway
    try:
        client = await GatewayClient.from_network(
            handle=net.handle, network_name=network_name,
            password=password,
        )
    except PermissionError:
        if password is None:
            password = getpass.getpass(f"Password for {net.handle}@{network_name}: ")
        client = await GatewayClient.from_network(
            handle=net.handle, network_name=network_name,
            password=password,
        )
    await client.connect()
    return client, net


def _fmt_age(unix_seconds: float | None) -> str:
    """Render a unix timestamp as a short relative age."""
    if not unix_seconds:
        return ""
    import time as _time
    delta = max(0, _time.time() - unix_seconds)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _fmt_expires(unix_seconds: float | None) -> str:
    if not unix_seconds:
        return ""
    import time as _time
    delta = unix_seconds - _time.time()
    if delta < 0:
        return "expired"
    if delta < 3600:
        return f"in {int(delta // 60)}m"
    if delta < 86400:
        return f"in {int(delta // 3600)}h"
    return f"in {int(delta // 86400)}d"


@cli.command("users")
@click.option("--network", "network_name", default=None,
              help="Which network to query (default: active)")
@click.option("--password", default=None,
              help="Password (omit to be prompted only if the cached cert is stale).")
def users_cmd(network_name: str | None, password: str | None):
    """List users registered in this network (authed)."""
    asyncio.run(_run_users_cli(network_name, password))


async def _run_users_cli(network_name: str | None, password: str | None):
    client, net = await _open_gateway_for_rest(network_name, password)
    if client is None:
        return
    try:
        data = await client.rest_get("/api/network/users")
        users = data.get("users") or []
        if not users:
            console.print("[dim]No users registered.[/dim]")
            return
        table = Table(title=f"Users in {net.name}")
        table.add_column("Handle", style="cyan")
        table.add_column("Status")
        table.add_column("PAKE", style="dim")
        table.add_column("Joined", style="dim")
        for u in users:
            table.add_row(
                u.get("handle", ""), u.get("status", ""),
                u.get("pake_algo", ""), _fmt_age(u.get("created_at")),
            )
        console.print(table)
    finally:
        await client.close()


@cli.command("members")
@click.option("--network", "network_name", default=None,
              help="Which network to query (default: active)")
@click.option("--password", default=None)
def members_cmd(network_name: str | None, password: str | None):
    """Show users + agents on this network in one view."""
    asyncio.run(_run_members_cli(network_name, password))


async def _run_members_cli(network_name: str | None, password: str | None):
    client, net = await _open_gateway_for_rest(network_name, password)
    if client is None:
        return
    try:
        users_data, agents_data = await asyncio.gather(
            client.rest_get("/api/network/users"),
            client.rest_get("/api/network/agents"),
        )
        utable = Table(title=f"Users on {net.name}")
        utable.add_column("Handle", style="cyan")
        utable.add_column("Status")
        utable.add_column("Joined", style="dim")
        for u in users_data.get("users", []) or []:
            utable.add_row(u.get("handle", ""), u.get("status", ""),
                           _fmt_age(u.get("created_at")))
        console.print(utable)

        atable = Table(title=f"Agents on {net.name}")
        atable.add_column("Handle", style="cyan")
        atable.add_column("NodeId", style="dim")
        atable.add_column("Last seen", style="dim")
        for a in agents_data.get("agents", []) or []:
            atable.add_row(
                a.get("handle", ""),
                (a.get("node_id") or "")[:24] + "…",
                _fmt_age(a.get("last_seen")),
            )
        console.print(atable)
    finally:
        await client.close()


@cli.command("invitations")
@click.option("--network", "network_name", default=None)
@click.option("--password", default=None)
def invitations_cmd(network_name: str | None, password: str | None):
    """List active invite codes on this network (coordinator-only)."""
    asyncio.run(_run_invitations_cli(network_name, password))


async def _run_invitations_cli(network_name: str | None, password: str | None):
    client, net = await _open_gateway_for_rest(network_name, password)
    if client is None:
        return
    try:
        data = await client.rest_get("/api/network/invitations")
        invs = data.get("invitations") or []
        if not invs:
            console.print("[dim]No active invitations.[/dim]")
            return
        table = Table(title=f"Active invitations on {net.name}")
        table.add_column("Code", style="cyan")
        table.add_column("For")
        table.add_column("Expires", style="dim")
        table.add_column("Minted by", style="dim")
        for inv in invs:
            bind = inv.get("bind_to") or ""
            role = inv.get("role") or ""
            # Operator-readable label: hide the role jargon when there's
            # a bound handle (the audience already conveys intent).
            audience = (
                f"new device for {bind}" if role == "device" and bind
                else f"onboard {bind}" if bind
                else "any new user" if role == "user"
                else f"agent (owner={inv.get('bind_to') or 'system'})" if role == "agent"
                else role
            )
            table.add_row(
                inv.get("code", ""),
                audience,
                _fmt_expires(inv.get("expires_at")),
                inv.get("created_by", ""),
            )
        console.print(table)
    finally:
        await client.close()


@cli.command("invite")
@click.argument("handle", required=False, default=None)
@click.option("--network", "network_name", default=None,
              help="Which network to mint on (default: active)")
@click.option("--ttl", default=7 * 24 * 3600, show_default=True, type=int,
              help="Invite TTL in seconds (max 90 days).")
@click.option("--password", default=None)
@click.option("--role", default=None, hidden=True,
              help="Advanced: force user|device|agent. Defaults to auto-detect.")
def invite_cmd(handle: str | None, network_name: str | None,
               ttl: int, password: str | None, role: str | None):
    """Mint an invite ticket.

    \b
      openagent-cli invite                 # open invite, anyone joins
      openagent-cli invite marco           # auto: onboard marco (new user)
      openagent-cli invite alessandro      # auto: new-device invite for alessandro
    """
    asyncio.run(_run_invite_cli(handle, network_name, ttl, password, role))


async def _run_invite_cli(handle: str | None, network_name: str | None,
                          ttl: int, password: str | None, role: str | None):
    client, net = await _open_gateway_for_rest(network_name, password)
    if client is None:
        return
    try:
        body: dict = {"ttl": ttl}
        if handle is not None:
            body["handle"] = handle
        if role is not None:
            body["role"] = role
        data = await client.rest_post("/api/network/invitations", data=body)
        if "error" in data:
            console.print(f"[red]{data['error']}[/red]")
            return
        console.print()
        console.print(
            f"[green]Invite ticket[/green] — [dim]{data.get('intent','')}, "
            f"expires {_fmt_expires(data.get('expires_at'))}[/dim]"
        )
        console.print(f"\n  [bold cyan]{data.get('ticket','')}[/bold cyan]\n")
        console.print(
            f"  Redeem with: [cyan]openagent-cli connect {data.get('ticket','')}[/cyan]\n"
        )
    finally:
        await client.close()


# ── Interactive terminal (PTY over the gateway — like SSH) ───────────────


@cli.command("terminal")
@click.option("--network", "network_name", default=None,
              help="Which network's agent to open a terminal on (default: active)")
@click.option("--password", default=None,
              help="Password (omit to be prompted only if the cached cert is stale).")
@click.option("--shell", "shell_path", default=None,
              help="Shell to launch (default: the server's $SHELL).")
@click.option("--cwd", default=None,
              help="Initial working directory on the server host.")
def terminal_cmd(network_name: str | None, password: str | None,
                 shell_path: str | None, cwd: str | None):
    """Open an interactive terminal on the agent's host — like an SSH shell.

    Spawns a real PTY on the machine running the OpenAgent server and
    bridges it to your local terminal: full-screen apps (vim, htop, top,
    REPLs) work exactly as they would over SSH. Type ``exit`` to end the
    shell, or press Ctrl-] to detach and leave it for the server to reap.
    """
    asyncio.run(_run_terminal_cli(network_name, password, shell_path, cwd))


async def _run_terminal_cli(network_name: str | None, password: str | None,
                            shell_path: str | None, cwd: str | None):
    client, net = await _open_gateway_for_rest(network_name, password)
    if client is None:
        return
    try:
        agent = client.target_handle or net.name
        console.print(
            f"[green]Terminal[/green] on [cyan]{agent}[/cyan] — "
            "[dim]Ctrl-] to detach, or type 'exit' to close the shell.[/dim]"
        )
        await _run_terminal(client, shell=shell_path, cwd=cwd)
    finally:
        await client.disconnect()


async def _run_terminal(
    client: GatewayClient, *, shell: str | None = None, cwd: str | None = None,
) -> None:
    """Bridge the local TTY to a gateway PTY in raw mode.

    Local stdin is put in raw mode and every byte is forwarded to the
    remote PTY (so Ctrl-C interrupts the *remote* foreground job, not
    this client); remote output bytes are written straight to stdout to
    preserve colour and cursor control. The escape hatch is Ctrl-] —
    it detaches locally without killing the shell. SIGWINCH is mirrored
    to the remote so resizing the window reflows full-screen apps.
    """
    import base64
    import shutil
    import uuid
    from src.client import TERMINAL_OUTPUT, TERMINAL_EXIT, TERMINAL_ERROR  # lazy import

    if os.name != "posix":
        console.print("[red]The terminal command needs a POSIX terminal (macOS/Linux).[/red]")
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print("[red]The terminal command needs an interactive TTY (not a pipe).[/red]")
        return

    import signal as _signal
    import termios
    import tty

    terminal_id = uuid.uuid4().hex[:16]
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    meta: dict[str, Any] = {}

    def term_size() -> tuple[int, int]:
        sz = shutil.get_terminal_size(fallback=(80, 24))
        return sz.columns, sz.lines

    def on_frame(frame: dict) -> None:
        t = frame.get("type")
        if t == TERMINAL_OUTPUT:
            try:
                payload = base64.b64decode(frame.get("data") or "")
            except Exception:  # noqa: BLE001
                return
            # Loop until every byte lands — a single os.write can short-
            # write a large burst even on a blocking stdout.
            while payload:
                try:
                    n = os.write(stdout_fd, payload)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                payload = payload[n:]
        elif t == TERMINAL_EXIT:
            meta["exit_code"] = frame.get("exit_code")
            meta["signal"] = frame.get("signal")
            done.set()
        elif t == TERMINAL_ERROR:
            meta["error"] = frame.get("error")
            done.set()
        # TERMINAL_READY: nothing to do — the prompt arrives as output.

    DETACH = 0x1D  # Ctrl-]

    def on_stdin() -> None:
        try:
            data = os.read(stdin_fd, 4096)
        except OSError:
            return
        if not data:
            return
        if DETACH in data:
            meta["detached"] = True
            done.set()
            return
        asyncio.ensure_future(client.send_terminal_input(terminal_id, data))

    def on_winch() -> None:
        cols, rows = term_size()
        asyncio.ensure_future(client.send_terminal_resize(terminal_id, cols, rows))

    client.set_terminal_handler(on_frame)
    old_attrs = termios.tcgetattr(stdin_fd)
    reader_added = False
    winch_added = False
    try:
        cols, rows = term_size()
        await client.send_terminal_open(
            terminal_id, cols=cols, rows=rows, cwd=cwd, shell=shell,
        )
        tty.setraw(stdin_fd)
        loop.add_reader(stdin_fd, on_stdin)
        reader_added = True
        try:
            loop.add_signal_handler(_signal.SIGWINCH, on_winch)
            winch_added = True
        except (NotImplementedError, ValueError):
            pass
        # Wait for the shell to end (or a detach), but wake periodically
        # so a dropped gateway connection doesn't hang the client forever.
        while not done.is_set():
            if not client.is_connected:
                meta.setdefault("error", "connection closed")
                break
            try:
                await asyncio.wait_for(done.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
    finally:
        if reader_added:
            try:
                loop.remove_reader(stdin_fd)
            except (ValueError, OSError):
                pass
        if winch_added:
            try:
                loop.remove_signal_handler(_signal.SIGWINCH)
            except (NotImplementedError, ValueError):
                pass
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        client.set_terminal_handler(None)
        # Detach leaves the shell for the server to reap on disconnect;
        # an explicit close is courteous so it doesn't linger.
        if meta.get("detached"):
            try:
                await client.send_terminal_close(terminal_id)
            except Exception:  # noqa: BLE001
                pass

    # Back in cooked mode — print a one-line epilogue.
    if meta.get("error"):
        console.print(f"\n[red]Terminal error: {meta['error']}[/red]")
    elif meta.get("detached"):
        console.print("\n[dim]Detached. The shell keeps running on the server until you disconnect.[/dim]")
    elif meta.get("signal"):
        console.print(f"\n[dim]Terminal exited (signal {meta['signal']}).[/dim]")
    else:
        console.print(f"\n[dim]Terminal exited (code {meta.get('exit_code')}).[/dim]")


async def _interactive_loop(client: GatewayClient, *, network_name: str, handle: str):
    """Drop-in for the legacy ``_interactive`` body — runs the REPL."""
    target = client.target_handle or "agent"
    console.print(Panel(
        f"[bold]{client.agent_name}[/bold] v{client.agent_version}\n"
        f"Network: [cyan]{network_name}[/cyan]   You: [cyan]{handle}[/cyan]   "
        f"Agent: [cyan]{target}[/cyan]",
        title="Connected", border_style="green",
    ))
    console.print("[dim]Type /help for commands.[/dim]\n")

    sessions = {"cli-default": "Default"}
    active = "cli-default"

    # Hydrate from the server's persisted session list.
    try:
        data = await client.rest_get("/api/sessions")
        for entry in data.get("sessions", []) or []:
            sid = entry.get("session_id")
            title = entry.get("title") or "Chat"
            framework = entry.get("framework", "")
            model = entry.get("model", "")
            extra = []
            if framework:
                extra.append(framework)
            if model:
                extra.append(model)
            label = title
            if extra:
                label = f"{title}  [dim]({' / '.join(extra)})[/dim]"
            if sid and sid not in sessions:
                sessions[sid] = label
        if len(sessions) > 1:
            # Pick the most recent server session as active.
            first_server = data["sessions"][0]["session_id"]
            if first_server in sessions:
                active = first_server
    except Exception:
        pass

    while True:
        try:
            user_input = Prompt.ask("[bold]You[/bold]")
        except (EOFError, KeyboardInterrupt):
            break

        text = user_input.strip()
        if not text:
            continue

        # ── Local CLI commands ──
        if text in ("/quit", "/exit", "/q"):
            break

        if text == "/help":
            _print_help()
            continue

        if text == "/sessions":
            if len(sessions) <= 1 and next(iter(sessions)) == "cli-default":
                console.print("[dim]No sessions yet. Start typing to create one.[/dim]")
            else:
                for sid, label in sessions.items():
                    marker = "→ " if sid == active else "  "
                    sid_short = sid[-12:] if len(sid) > 12 else sid
                    console.print(f"{marker}[cyan]{sid_short}[/cyan] {label}")
            continue

        if text.startswith("/switch "):
            target = text.split(" ", 1)[1].strip()
            found = [s for s in sessions if s.endswith(target)]
            if found:
                active = found[0]
                console.print(f"Switched to session [cyan]{active[-8:]}[/cyan]")
            else:
                console.print(f"[red]No session matching '{target}'[/red]")
            continue

        if text == "/new":
            result = await client.send_command("new")
            session_id = f"cli-{len(sessions)}"
            sessions[session_id] = f"Chat {len(sessions)}"
            active = session_id
            console.print(f"[green]{result}[/green]")
            continue

        if text.startswith("/rename "):
            parts = text.split(" ", 2)
            if len(parts) < 3:
                console.print("[red]Usage: /rename <id_suffix> <new name>[/red]")
                continue
            target = parts[1].strip()
            new_name = parts[2].strip()
            found = [s for s in sessions if s.endswith(target)]
            if not found:
                console.print(f"[red]No session matching '{target}'[/red]")
                continue
            sid = found[0]
            sessions[sid] = new_name
            try:
                await client.rest_patch(f"/api/sessions/{sid}", {"title": new_name})
                console.print(f"[green]Renamed to '{new_name}'[/green]")
            except Exception as e:
                console.print(f"[yellow]Renamed locally (server update failed: {e})[/yellow]")
            continue

        if text.startswith("/delete "):
            target = text.split(" ", 1)[1].strip()
            found = [s for s in sessions if s.endswith(target)]
            if not found:
                console.print(f"[red]No session matching '{target}'[/red]")
                continue
            sid = found[0]
            label = sessions[sid]
            # Deleting a chat also removes the sub-agent sessions it spawned —
            # warn before the typed-yes confirmation so it's never a surprise.
            console.print(
                "[dim]This also deletes any sub-agent sessions this chat "
                "spawned.[/dim]"
            )
            confirm = Prompt.ask(
                f"[yellow]Delete session '{label}'?[/yellow] Type [bold]yes[/bold] to confirm",
            )
            if confirm.strip().lower() != "yes":
                console.print("[dim]Cancelled[/dim]")
                continue
            del sessions[sid]
            if active == sid:
                active = next(iter(sessions), "cli-default")
            try:
                res = await client.rest_delete(f"/api/sessions/{sid}")
                if isinstance(res, dict) and res.get("error"):
                    console.print(f"[red]{res['error']}[/red]")
                else:
                    # The server reports how many rows it removed (the chat
                    # plus any cascaded sub-agents).
                    count = res.get("deleted_count") if isinstance(res, dict) else None
                    if isinstance(count, int) and count > 1:
                        console.print(
                            f"[green]Deleted '{label}' and {count - 1} "
                            f"sub-agent session(s)[/green]"
                        )
                    else:
                        console.print(f"[green]Deleted '{label}'[/green]")
            except Exception as e:
                console.print(f"[yellow]Deleted locally (server cleanup failed: {e})[/yellow]")
            continue

        if text.startswith("/file "):
            rest = text.split(" ", 1)[1].strip()
            try:
                paths = shlex.split(rest)
            except ValueError:
                paths = rest.split()
            if paths:
                await _send_files(client, paths, active)
            else:
                console.print("[red]Usage: /file <path> [more paths...][/red]")
            continue

        if text == "/vault":
            await _vault_menu(client)
            continue

        if text == "/config":
            await _config_menu(client)
            continue

        if text == "/tasks":
            await _tasks_menu(client)
            continue

        if text in ("/workflows", "/workflow"):
            await _workflows_menu(client)
            continue

        if text == "/mcps":
            await _mcps_menu(client)
            continue

        if text in ("/terminal", "/shell"):
            console.print(
                "[dim]Opening terminal — Ctrl-] to detach, type 'exit' to close.[/dim]"
            )
            await _run_terminal(client)
            continue

        if text in ("/models", "/model"):
            await _models_menu(client)
            continue

        if text == "/providers":
            await _providers_menu(client)
            continue

        if text == "/usage":
            await _usage_menu(client)
            continue

        if text == "/settings":
            await _settings_menu(client)
            continue

        # ── /stop — barge-in the active session's live turn ──
        # Routes to the stream ``interrupt`` frame (the verb that actually
        # cancels a StreamSession turn) rather than the legacy COMMAND
        # ``stop`` that targets an unused queue. Typed ``/stop`` is only
        # reachable between turns here (the REPL blocks during a turn —
        # use Ctrl-C to stop mid-stream), so this is mostly a no-op safety
        # net, but it now hits the correct path.
        if text == "/stop":
            try:
                await client.send_interrupt(active, reason="manual")
                console.print("[dim]Stop requested.[/dim]")
            except Exception as e:
                console.print(f"[red]Command failed: {e}[/red]")
            continue

        # ── Gateway pass-through commands ──
        # /clear, /restart, /update, /status, /queue, /reset,
        # /compact, /model [arg]
        if text.startswith("/"):
            parts = text[1:].split(None, 1)  # split into [cmd, rest] or [cmd]
            cmd = parts[0]
            cmd_arg = parts[1] if len(parts) > 1 else None
            # Session-scoped commands need session_id forwarded
            session_scoped = {"compact", "model", "clear", "stop", "new", "reset"}
            sid_for_cmd = active if cmd in session_scoped else None
            try:
                result = await client.send_command(cmd, arg=cmd_arg, session_id=sid_for_cmd)
                console.print(f"[dim]{result}[/dim]")
            except Exception as e:
                console.print(f"[red]Command failed: {e}[/red]")
            continue

        # ── Chat message ──
        await _send_message_with_indicator(client, text, active)

    await client.disconnect()
    console.print("[dim]Disconnected.[/dim]")


# ── Vault menu ───────────────────────────────────────────────────────────

def _print_write_result(res: dict, path: str, verb: str = "Saved") -> None:
    """Print a vault write result, surfacing the quality gate: a rejected
    write (``blocked``) with its errors, the fields it auto-fixed
    (``applied``), warnings, and the git commit."""
    if res.get("blocked") or res.get("ok") is False:
        console.print(
            f"[red]✕ {path} rejected by the vault quality gate — not saved:[/red]")
        for e in (res.get("errors") or []):
            console.print(f"  [red]- {e.get('rule')}: {e.get('message')}[/red]")
        for w in (res.get("warnings") or []):
            console.print(f"  [yellow]⚠ {w.get('rule')}: {w.get('message')}[/yellow]")
        console.print("[dim]Fix the above and save again.[/dim]")
        return
    line = f"[green]{verb} {path}[/green]"
    commit = res.get("commit")
    if commit:
        line += f" [dim](committed {str(commit)[:8]})[/dim]"
    console.print(line)
    for a in (res.get("applied") or []):
        console.print(f"  [dim]✓ auto-fixed: {a}[/dim]")
    for w in (res.get("warnings") or []):
        sev = w.get("severity", "warn")
        color = "red" if sev == "error" else "yellow"
        console.print(f"  [{color}]⚠ {w.get('rule')}: {w.get('message')}[/{color}]")


async def _vault_menu(client: GatewayClient):
    """Interactive vault browser with search, edit, gate, history, and a
    link-safe rename."""
    while True:
        console.print(
            "\n[bold]Vault[/bold] — [cyan]l[/cyan]ist, [cyan]s[/cyan]earch, "
            "[cyan]e[/cyan]dit, [cyan]n[/cyan]ew, [cyan]m[/cyan]ove/rename, "
            "[cyan]g[/cyan]ate, [cyan]h[/cyan]istory, [cyan]d[/cyan]elete, "
            "[cyan]q[/cyan]uit")
        action = Prompt.ask(
            "Action", choices=["l", "s", "e", "n", "m", "g", "h", "d", "q"],
            default="l")
        if action == "q":
            return

        if action == "l":
            await _vault_list_and_open(client)
        elif action == "s":
            query = Prompt.ask("Search")
            if not query.strip():
                continue
            data = await client.rest_get(f"/api/vault/search?q={query}")
            results = data.get("results", [])
            if not results:
                console.print("[dim]No matches.[/dim]")
                continue
            await _vault_pick_and_open(client, results, title=f"Search: {query}")
        elif action == "e":
            path = Prompt.ask("Note path (e.g. notes/foo.md)")
            await _vault_edit(client, path.strip())
        elif action == "n":
            path = Prompt.ask("New note path (e.g. notes/foo.md)").strip()
            if not path:
                continue
            new_text = _open_in_editor("# New note\n\n", suffix=".md")
            if new_text is None:
                continue
            res = await client.rest_put(f"/api/vault/notes/{path}", {"content": new_text})
            _print_write_result(res, path, verb="Created")
        elif action == "m":
            await _vault_move(client)
        elif action == "g":
            await _vault_gate(client)
        elif action == "h":
            await _vault_history(client)
        elif action == "d":
            path = Prompt.ask("Note path to delete").strip()
            if not path:
                continue
            if not Confirm.ask(f"Delete '{path}'?", default=False):
                continue
            res = await client.rest_delete(f"/api/vault/notes/{path}")
            if res.get("ok"):
                console.print(f"[green]Deleted {path}[/green]")
            else:
                console.print(f"[red]Failed: {res}[/red]")


async def _vault_list_and_open(client: GatewayClient):
    data = await client.rest_get("/api/vault/notes")
    notes = data.get("notes", [])
    await _vault_pick_and_open(client, notes, title=f"Vault ({len(notes)} notes)")


async def _vault_pick_and_open(client: GatewayClient, notes: list[dict], title: str):
    if not notes:
        console.print("[dim]No notes.[/dim]")
        return
    table = Table(title=title)
    table.add_column("#", width=3)
    table.add_column("Title")
    table.add_column("Path", style="dim")
    table.add_column("Tags", style="dim")
    for i, n in enumerate(notes):
        tags = ", ".join(n.get("tags", [])[:3])
        table.add_row(str(i + 1), n.get("title", n.get("path", "")), n.get("path", ""), tags)
    console.print(table)

    choice = Prompt.ask("Open # (or 'e<#>' to edit, 'q' to back)", default="q")
    choice = choice.strip().lower()
    if choice in ("q", ""):
        return
    edit_mode = choice.startswith("e")
    num_str = choice[1:] if edit_mode else choice
    try:
        idx = int(num_str) - 1
        note = notes[idx]
    except (ValueError, IndexError):
        console.print("[red]Invalid selection[/red]")
        return

    if edit_mode:
        await _vault_edit(client, note["path"])
    else:
        data = await client.rest_get(f"/api/vault/notes/{note['path']}")
        body = data.get("body") or data.get("content", "")
        console.print(Panel(Markdown(body), title=note["path"]))


async def _vault_edit(client: GatewayClient, path: str):
    data = await client.rest_get(f"/api/vault/notes/{path}")
    if data.get("error"):
        console.print(f"[red]{data.get('error')}[/red]")
        return
    current = data.get("content", "")
    new_text = _open_in_editor(current, suffix=".md")
    if new_text is None:
        return
    res = await client.rest_put(f"/api/vault/notes/{path}", {"content": new_text})
    _print_write_result(res, path, verb="Saved")


async def _vault_move(client: GatewayClient):
    """Rename/move a note or folder — the server rewrites every inbound
    [[wikilink]] so nothing breaks (uses POST /api/vault/move, NOT
    delete+create)."""
    old = Prompt.ask("Move from (note or folder path)").strip()
    new = Prompt.ask("Move to").strip()
    if not old or not new:
        return
    res = await client.rest_post("/api/vault/move", {"from": old, "to": new})
    if res.get("error"):
        console.print(f"[red]{res['error']}[/red]")
        return
    commit = res.get("commit")
    suffix = f" [dim](committed {str(commit)[:8]})[/dim]" if commit else ""
    console.print(
        f"[green]Moved {res.get('notes_moved', '?')} note(s); rewrote "
        f"{res.get('links_rewritten', 0)} link(s) across "
        f"{res.get('notes_updated', 0)} note(s).[/green]{suffix}")


async def _vault_gate(client: GatewayClient):
    """Run the quality gate and show the report."""
    rep = await client.rest_get("/api/vault/gate")
    ok = rep.get("ok")
    color = "green" if ok else "red"
    console.print(
        f"[{color}]{rep.get('error_count', 0)} errors, "
        f"{rep.get('warn_count', 0)} warnings, {rep.get('info_count', 0)} info "
        f"across {rep.get('note_count', 0)} notes[/{color}]")
    by_rule = rep.get("by_rule") or {}
    for rule in sorted(by_rule):
        console.print(f"  [dim]{rule}[/dim]: {len(by_rule[rule])}")


def _provenance_who(prov: dict) -> str:
    who = prov.get("origin", "")
    for k in ("session", "workflow", "task", "tool"):
        if prov.get(k):
            who += f" {prov[k]}"
            break
    return who.strip()


async def _vault_history(client: GatewayClient):
    """Vault git history with provenance — inspect a commit's changes, then
    optionally restore the vault to that state or reset to it."""
    data = await client.rest_get("/api/vault/history?limit=25")
    commits = data.get("commits", [])
    if not commits:
        console.print("[dim]No history (git tracking may be disabled).[/dim]")
        return
    table = Table(title="Vault history")
    table.add_column("#", width=3)
    table.add_column("Commit", style="dim", width=9)
    table.add_column("Change")
    table.add_column("By", style="cyan")
    table.add_column("When", style="dim")
    for i, c in enumerate(commits):
        table.add_row(str(i + 1), c.get("hash", ""), c.get("subject", ""),
                      _provenance_who(c.get("provenance") or {}),
                      (c.get("date", "") or "")[:10])
    console.print(table)
    choice = Prompt.ask(
        "Inspect # (view changes / restore / reset), or 'q'", default="q")
    choice = choice.strip().lower()
    if choice in ("q", ""):
        return
    try:
        commit = commits[int(choice) - 1]
    except (ValueError, IndexError):
        console.print("[red]Invalid selection[/red]")
        return
    await _vault_commit_detail(client, commit)


def _print_diff(diff: str) -> None:
    """Print a unified diff with +/- colouring (markup disabled so diff
    content like ``[[wikilinks]]`` isn't parsed as rich markup)."""
    for line in diff.splitlines():
        style = "dim"
        if line.startswith("+") and not line.startswith("+++"):
            style = "green"
        elif line.startswith("-") and not line.startswith("---"):
            style = "red"
        elif line.startswith("@@"):
            style = "cyan"
        console.print(line, style=style, markup=False, highlight=False)


async def _vault_commit_detail(client: GatewayClient, commit: dict):
    """Show a commit's changes (files + diff) and offer restore / reset."""
    h = commit.get("hash", "")
    det = await client.rest_get(f"/api/vault/commit?hash={h}")
    if det.get("error"):
        console.print(f"[red]{det['error']}[/red]")
        return
    console.print(
        f"\n[bold]{det.get('subject', '')}[/bold]  [dim]{det.get('hash', '')} · "
        f"{(det.get('date', '') or '')[:19]} · {det.get('author', '')}[/dim]")
    prov = det.get("provenance") or {}
    if prov:
        console.print(f"[dim]by {_provenance_who(prov) or 'system'}[/dim]")
    for f in det.get("files", []):
        console.print(f"  [yellow]{f.get('status', '?')}[/yellow] {f.get('path', '')}")
    if det.get("diff"):
        console.print()
        _print_diff(det["diff"])
        if det.get("diff_truncated"):
            console.print("[dim]… diff truncated[/dim]")

    console.print(
        "\n[bold]Actions[/bold] — [cyan]r[/cyan]estore this state "
        "(safe: adds a commit, keeps history), rese[cyan]t[/cyan] to here "
        "([red]DESTRUCTIVE[/red]: deletes every later commit), [cyan]q[/cyan]uit")
    act = Prompt.ask("Action", choices=["r", "t", "q"], default="q")
    if act == "r":
        if not Confirm.ask(
                f"Restore the vault to the state at {h[:8]}? "
                "(adds a new commit; history is kept)", default=False):
            return
        res = await client.rest_post("/api/vault/restore", {"hash": h})
        if res.get("error"):
            console.print(f"[red]{res['error']}[/red]")
        elif res.get("changed"):
            console.print(
                f"[green]Restored to {h[:8]} "
                f"(new commit {str(res.get('commit') or '')[:8]}).[/green]")
        else:
            console.print("[dim]Vault already at that state — nothing to do.[/dim]")
    elif act == "t":
        console.print(
            f"[red]⚠ This permanently deletes every commit AFTER {h[:8]}. "
            "It cannot be undone from the app.[/red]")
        if not Confirm.ask("Proceed?", default=False):
            return
        typed = Prompt.ask(f"Type the short hash [bold]{h[:8]}[/bold] to confirm")
        if typed.strip() != h[:8]:
            console.print("[dim]Hash mismatch — aborted.[/dim]")
            return
        res = await client.rest_post(
            "/api/vault/reset", {"hash": h, "confirm": True})
        if res.get("error"):
            console.print(f"[red]{res['error']}[/red]")
        else:
            console.print(
                f"[green]Reset to {h[:8]} — deleted "
                f"{res.get('deleted', 0)} later commit(s).[/green]")


# ── Config / settings ────────────────────────────────────────────────────

async def _config_menu(client: GatewayClient):
    cfg = await client.rest_get("/api/config")
    table = Table(title="Configuration")
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_row("name", str(cfg.get("name", "")))
    # Providers, MCPs, models, and scheduled tasks all live in SQLite —
    # use /mcps, /models, /tasks (and ``openagent provider``) to inspect
    # and edit them.
    channels = cfg.get("channels", {})
    table.add_row("channels", ", ".join(channels.keys()) if channels else "none")
    table.add_row("dream_mode", str(cfg.get("dream_mode", {}).get("enabled", False)))
    table.add_row("auto_update", str(cfg.get("auto_update", {}).get("enabled", False)))
    console.print(table)


async def _settings_menu(client: GatewayClient):
    """Edit identity, system prompt, channels, dream_mode, auto_update."""
    while True:
        cfg = await client.rest_get("/api/config")
        console.print("\n[bold]Settings[/bold]")
        console.print(f"  1) name: [cyan]{cfg.get('name', '')}[/cyan]")
        console.print(f"  2) system_prompt: [dim]{(cfg.get('system_prompt', '') or '')[:60]}{'...' if len(cfg.get('system_prompt','') or '') > 60 else ''}[/dim]")
        console.print(f"  3) channels (gateway/telegram/discord/whatsapp)")
        dm = cfg.get("dream_mode", {}) or {}
        console.print(f"  4) dream_mode: enabled={dm.get('enabled', False)} time={dm.get('time', '')}")
        au = cfg.get("auto_update", {}) or {}
        console.print(f"  5) auto_update: enabled={au.get('enabled', False)} mode={au.get('mode', '')} interval={au.get('check_interval', '')}")
        console.print(f"  q) back")
        choice = Prompt.ask("Edit", choices=["1", "2", "3", "4", "5", "q"], default="q")

        if choice == "q":
            return

        if choice == "1":
            new = Prompt.ask("Agent name", default=str(cfg.get("name", "")))
            await client.rest_patch("/api/config/name", new)
            console.print("[green]Saved. Restart required.[/green]")

        elif choice == "2":
            current = cfg.get("system_prompt", "") or ""
            new = _open_in_editor(current, suffix=".txt")
            if new is None:
                continue
            await client.rest_patch("/api/config/system_prompt", new)
            console.print("[green]Saved. Restart required.[/green]")

        elif choice == "3":
            await _channels_submenu(client, cfg)

        elif choice == "4":
            enabled = Confirm.ask("Enable dream mode?", default=dm.get("enabled", False))
            time_val = Prompt.ask("Time (HH:MM)", default=dm.get("time", "03:00"))
            await client.rest_patch("/api/config/dream_mode", {"enabled": enabled, "time": time_val})
            console.print("[green]Saved. Restart required.[/green]")

        elif choice == "5":
            enabled = Confirm.ask("Enable auto-update?", default=au.get("enabled", False))
            mode = Prompt.ask("Mode", choices=["auto", "notify", "manual"], default=au.get("mode", "notify"))
            interval = Prompt.ask("Check interval (cron)", default=au.get("check_interval", "17 */6 * * *"))
            await client.rest_patch("/api/config/auto_update", {"enabled": enabled, "mode": mode, "check_interval": interval})
            console.print("[green]Saved. Restart required.[/green]")


async def _channels_submenu(client: GatewayClient, cfg: dict):
    channels = cfg.get("channels", {}) or {}
    # The "g" (gateway/websocket) branch is gone — gateway transport
    # is now Iroh + handle@network credentials managed outside this
    # menu (`openagent network` subcommands and `openagent-cli connect`).
    console.print("\n[bold]Channels[/bold]: t(elegram), d(iscord), w(hatsApp)")
    which = Prompt.ask("Edit", choices=["t", "d", "w", "q"], default="q")
    if which == "q":
        return

    if which == "t":
        tg = channels.get("telegram", {}) or {}
        token = Prompt.ask("Bot token (blank to disable)", default=tg.get("token", ""))
        allowed = Prompt.ask("Allowed user IDs (comma-separated)", default=",".join(map(str, tg.get("allowed_users", []))))
        model = Prompt.ask("Model override (blank = default)", default=tg.get("model", ""))
        new_channels = dict(channels)
        if token.strip():
            new_channels["telegram"] = {
                "token": token,
                "allowed_users": [u.strip() for u in allowed.split(",") if u.strip()],
                "model": model.strip() or None,
            }
        else:
            new_channels.pop("telegram", None)
        await client.rest_patch("/api/config/channels", new_channels)

    elif which == "d":
        dc = channels.get("discord", {}) or {}
        token = Prompt.ask("Bot token (blank to disable)", default=dc.get("token", ""))
        allowed = Prompt.ask("Allowed user IDs (comma-separated)", default=",".join(map(str, dc.get("allowed_users", []))))
        model = Prompt.ask("Model override (blank = default)", default=dc.get("model", ""))
        new_channels = dict(channels)
        if token.strip():
            new_channels["discord"] = {
                "token": token,
                "allowed_users": [u.strip() for u in allowed.split(",") if u.strip()],
                "model": model.strip() or None,
            }
        else:
            new_channels.pop("discord", None)
        await client.rest_patch("/api/config/channels", new_channels)

    elif which == "w":
        wa = channels.get("whatsapp", {}) or {}
        gid = Prompt.ask("Green API instance ID (blank to disable)", default=wa.get("green_api_id", ""))
        gtoken = Prompt.ask("Green API token (blank to disable)", default=wa.get("green_api_token", ""))
        allowed = Prompt.ask("Allowed user numbers (comma-separated)", default=",".join(map(str, wa.get("allowed_users", []))))
        model = Prompt.ask("Model override (blank = default)", default=wa.get("model", ""))
        new_channels = dict(channels)
        if gid.strip() and gtoken.strip():
            new_channels["whatsapp"] = {
                "green_api_id": gid,
                "green_api_token": gtoken,
                "allowed_users": [u.strip() for u in allowed.split(",") if u.strip()],
                "model": model.strip() or None,
            }
        else:
            new_channels.pop("whatsapp", None)
        await client.rest_patch("/api/config/channels", new_channels)

    console.print("[green]Saved. Restart required for channel changes.[/green]")


# ── Run history (shared by workflows + scheduled tasks) ──────────────────


def _run_status_markup(status: str) -> str:
    """Colour a run status the same way across both history tables."""
    color = {
        "success": "green",
        "failed": "red",
        "running": "yellow",
        "cancelled": "dim",
    }.get(status, "white")
    return f"[{color}]{status}[/{color}]"


def _fmt_run_duration(run: dict) -> str:
    started = run.get("started_at")
    finished = run.get("finished_at")
    if started and finished:
        return f"{finished - started:.2f}s"
    if run.get("status") == "running":
        return "…"
    return "—"


def _run_detail(run: dict) -> str:
    """One-line detail cell: the error wins, else an output preview.

    Task runs carry a plain ``output`` string; workflow runs carry an
    ``outputs`` dict — collapse either into a short single line."""
    err = run.get("error")
    if err:
        return str(err).replace("\n", " ")[:80]
    out = run.get("output")
    if out is None:
        outputs = run.get("outputs")
        if outputs:
            out = json.dumps(outputs)
    if out:
        return str(out).replace("\n", " ")[:80]
    return ""


async def _show_runs(client: GatewayClient, base: str, item: dict, kind: str):
    """Fetch and print the recent run history for one workflow / task.

    ``base`` is the collection path (``/api/workflows`` or
    ``/api/scheduled-tasks``); ``item`` is the selected row. Mirrors the
    desktop app's RunHistoryDrawer as a terminal table (newest first).
    """
    name = item.get("name", "?")
    data = await client.rest_get(f"{base}/{item['id']}/runs?limit=20")
    if isinstance(data, dict) and data.get("error"):
        console.print(f"[red]{data['error']}[/red]")
        return
    runs = (data.get("runs") if isinstance(data, dict) else None) or []
    if not runs:
        console.print(f"[dim]No runs recorded for '{name}' yet.[/dim]")
        return

    table = Table(title=f"{kind} runs — {name} ({len(runs)})")
    table.add_column("#", width=3)
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Dur", justify="right")
    table.add_column("Trigger", style="dim")
    table.add_column("Detail", max_width=80)
    for i, r in enumerate(runs):
        table.add_row(
            str(i + 1),
            _run_status_markup(r.get("status", "?")),
            r.get("started_at_iso") or "—",
            _fmt_run_duration(r),
            r.get("trigger", "—"),
            _run_detail(r),
        )
    console.print(table)


# ── Tasks (cron) ─────────────────────────────────────────────────────────

async def _tasks_menu(client: GatewayClient):
    """Scheduled tasks — DB-backed via ``/api/scheduled-tasks``.

    Changes take effect within the scheduler's next tick (~30 s). No
    restart needed.
    """
    while True:
        data = await client.rest_get("/api/scheduled-tasks")
        tasks = data.get("tasks", []) or []

        table = Table(title=f"Scheduled Tasks ({len(tasks)})")
        table.add_column("#", width=3)
        table.add_column("Name", style="cyan")
        table.add_column("Cron")
        table.add_column("On", width=3)
        table.add_column("Live", width=4)
        table.add_column("Prompt", max_width=50)
        for i, t in enumerate(tasks):
            table.add_row(
                str(i + 1),
                t.get("name", "?"),
                t.get("cron_expression", "?"),
                "✓" if t.get("enabled") else "—",
                "[green]▶[/green]" if t.get("running") else "—",
                (t.get("prompt", ""))[:50],
            )
        console.print(table)
        console.print("[cyan]a[/cyan]dd, [cyan]e[/cyan]dit #, [cyan]r[/cyan]un #, [cyan]s[/cyan]top #, [cyan]h[/cyan]istory #, [cyan]d[/cyan]elete #, [cyan]t[/cyan]oggle #, [cyan]q[/cyan]uit")
        action = Prompt.ask("Action", default="q")
        action = action.strip().lower()

        if action in ("q", ""):
            return

        if action == "a":
            name = Prompt.ask("Name (e.g. health-check)").strip()
            cron = Prompt.ask("Cron (5-field, e.g. '*/30 * * * *')").strip()
            prompt = Prompt.ask("Prompt").strip()
            if not (name and cron and prompt):
                console.print("[red]All fields required[/red]")
                continue
            res = await client.rest_post(
                "/api/scheduled-tasks",
                {"name": name, "cron_expression": cron, "prompt": prompt},
            )
            if res.get("error"):
                console.print(f"[red]{res['error']}[/red]")
            else:
                console.print("[green]Added.[/green]")

        elif action.startswith("e") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(tasks):
                t = tasks[idx]
                name = Prompt.ask("Name", default=t.get("name", ""))
                cron = Prompt.ask("Cron", default=t.get("cron_expression", ""))
                prompt = Prompt.ask("Prompt", default=t.get("prompt", ""))
                res = await client.rest_patch(
                    f"/api/scheduled-tasks/{t['id']}",
                    {"name": name, "cron_expression": cron, "prompt": prompt},
                )
                if res.get("error"):
                    console.print(f"[red]{res['error']}[/red]")
                else:
                    console.print("[green]Saved.[/green]")

        elif action.startswith("r") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(tasks):
                t = tasks[idx]
                console.print(
                    f"[dim]Running '{t.get('name')}' now (wait up to 5 min)…[/dim]"
                )
                res = await client.rest_post(
                    f"/api/scheduled-tasks/{t['id']}/run",
                    {"wait": True, "timeout_s": 300},
                )
                if isinstance(res, dict) and res.get("error"):
                    console.print(f"[red]{res['error']}[/red]")
                else:
                    status = res.get("status") if isinstance(res, dict) else None
                    if status == "success":
                        console.print("[green]Run finished: success.[/green]")
                    elif status:
                        console.print(f"[yellow]Run finished: {status}.[/yellow]")
                    else:
                        console.print("[dim]Run dispatched.[/dim]")

        elif action.startswith("s") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(tasks):
                t = tasks[idx]
                console.print(
                    f"[dim]Stopping running firing(s) of '{t.get('name')}'…[/dim]"
                )
                res = await client.rest_post(
                    f"/api/scheduled-tasks/{t['id']}/stop",
                    {"wait": True, "timeout_s": 30},
                )
                if isinstance(res, dict) and res.get("error"):
                    console.print(f"[red]{res['error']}[/red]")
                else:
                    count = res.get("count", 0) if isinstance(res, dict) else 0
                    if count:
                        console.print(f"[green]Stopped {count} firing(s).[/green]")
                    else:
                        console.print("[dim]Nothing was running.[/dim]")

        elif action.startswith("h") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(tasks):
                await _show_runs(
                    client, "/api/scheduled-tasks", tasks[idx], "Task",
                )

        elif action.startswith("d") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(tasks):
                t = tasks[idx]
                if Confirm.ask(f"Delete '{t.get('name')}'?", default=False):
                    res = await client.rest_delete(f"/api/scheduled-tasks/{t['id']}")
                    if res.get("error"):
                        console.print(f"[red]{res['error']}[/red]")
                    else:
                        console.print("[green]Deleted.[/green]")

        elif action.startswith("t") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(tasks):
                t = tasks[idx]
                res = await client.rest_patch(
                    f"/api/scheduled-tasks/{t['id']}",
                    {"enabled": not t.get("enabled")},
                )
                if res.get("error"):
                    console.print(f"[red]{res['error']}[/red]")


# ── Workflows ────────────────────────────────────────────────────────────


def _fmt_concurrency(cap: int | None) -> str:
    """Render the concurrency cap as a compact cell. ``None`` is the
    default (unlimited); display it as ``∞`` so the column never
    becomes confusing whitespace."""
    if cap is None:
        return "∞"
    return f"≤{cap}"


async def _workflows_menu(client: GatewayClient):
    """Workflow registry — DB-backed via ``/api/workflows``.

    Visual editing of the graph lives in the desktop app; the CLI menu
    focuses on the cross-cutting toggles a terminal user actually
    needs: enable/disable, trigger a manual run, and the new
    ``max_concurrent_runs`` cap that decides how many overlapping runs
    of the same workflow may execute (empty / NULL = unlimited).
    """
    while True:
        data = await client.rest_get("/api/workflows")
        rows = data.get("workflows", []) or []

        table = Table(title=f"Workflows ({len(rows)})")
        table.add_column("#", width=3)
        table.add_column("Name", style="cyan")
        table.add_column("Triggers", style="dim")
        table.add_column("On", width=3)
        table.add_column("Concurrent", justify="center")
        table.add_column("Blocks", justify="right")
        for i, w in enumerate(rows):
            trig = ",".join(
                t.replace("trigger-", "") for t in (w.get("trigger_types") or [])
            ) or "—"
            cap = w.get("max_concurrent_runs")
            cap_cell = _fmt_concurrency(cap if cap is None else int(cap))
            nodes = (w.get("graph") or {}).get("nodes") or []
            table.add_row(
                str(i + 1),
                w.get("name", "?"),
                trig,
                "✓" if w.get("enabled") else "—",
                cap_cell,
                str(len(nodes)),
            )
        console.print(table)
        console.print(
            "[cyan]r[/cyan]un #, [cyan]h[/cyan]istory #, [cyan]t[/cyan]oggle #, "
            "[cyan]c[/cyan]oncurrency #, [cyan]d[/cyan]elete #, "
            "[cyan]q[/cyan]uit"
        )
        action = Prompt.ask("Action", default="q").strip().lower()

        if action in ("q", ""):
            return

        if action.startswith("r") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(rows):
                w = rows[idx]
                console.print(
                    f"[dim]Running '{w.get('name')}' (wait up to 5 min)…[/dim]"
                )
                res = await client.rest_post(
                    f"/api/workflows/{w['id']}/run",
                    {"wait": True, "timeout_s": 300},
                )
                if isinstance(res, dict) and res.get("error"):
                    console.print(f"[red]{res['error']}[/red]")
                else:
                    status = res.get("status") if isinstance(res, dict) else None
                    if status == "success":
                        console.print("[green]Run finished: success.[/green]")
                    elif status:
                        console.print(f"[yellow]Run finished: {status}.[/yellow]")
                    else:
                        console.print("[dim]Run dispatched.[/dim]")

        elif action.startswith("h") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(rows):
                await _show_runs(client, "/api/workflows", rows[idx], "Workflow")

        elif action.startswith("t") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(rows):
                w = rows[idx]
                res = await client.rest_patch(
                    f"/api/workflows/{w['id']}",
                    {"enabled": not w.get("enabled")},
                )
                if isinstance(res, dict) and res.get("error"):
                    console.print(f"[red]{res['error']}[/red]")

        elif action.startswith("c") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(rows):
                w = rows[idx]
                current_cap = w.get("max_concurrent_runs")
                current = "" if current_cap is None else str(int(current_cap))
                console.print(
                    "[dim]Max concurrent runs of this workflow. "
                    "Empty = unlimited (default), 1 = serial, N = up to N "
                    "at a time.[/dim]"
                )
                raw = Prompt.ask("Max concurrent runs", default=current)
                raw = raw.strip()
                if raw == "":
                    new_cap: int | None = None
                else:
                    try:
                        new_cap = int(raw)
                    except ValueError:
                        console.print(
                            "[red]Must be a whole number ≥ 1, or empty.[/red]"
                        )
                        continue
                    if new_cap < 1:
                        console.print(
                            "[red]Must be ≥ 1 (or empty for unlimited).[/red]"
                        )
                        continue
                res = await client.rest_patch(
                    f"/api/workflows/{w['id']}",
                    {"max_concurrent_runs": new_cap},
                )
                if isinstance(res, dict) and res.get("error"):
                    console.print(f"[red]{res['error']}[/red]")
                else:
                    console.print(
                        f"[green]Concurrency cap → {_fmt_concurrency(new_cap)}.[/green]"
                    )

        elif action.startswith("d") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(rows):
                w = rows[idx]
                if Confirm.ask(
                    f"Delete workflow '{w.get('name')}' and its run history?",
                    default=False,
                ):
                    res = await client.rest_delete(f"/api/workflows/{w['id']}")
                    if isinstance(res, dict) and res.get("error"):
                        console.print(f"[red]{res['error']}[/red]")
                    else:
                        console.print("[green]Deleted.[/green]")


# ── MCPs ─────────────────────────────────────────────────────────────────


async def _mcps_menu(client: GatewayClient):
    """MCP registry editor — DB-backed (hot-reloaded on the next message).

    Reads from /api/mcps (the ``mcps`` SQLite table). The yaml ``mcp:``
    section is now read-only: first-boot bootstrap copies it into the DB,
    subsequent edits go through this menu or the mcp-manager MCP and
    take effect without a restart.
    """
    while True:
        data = await client.rest_get("/api/mcps")
        rows = data.get("mcps", []) or []

        table = Table(title=f"MCPs ({len(rows)})")
        table.add_column("#", width=3)
        table.add_column("Name", style="cyan")
        table.add_column("Kind", style="dim")
        table.add_column("Status")
        table.add_column("Target", style="dim")
        for i, m in enumerate(rows):
            status = "[green]enabled[/green]" if m.get("enabled") else "[red]disabled[/red]"
            target_val = m.get("command") or m.get("url") or m.get("builtin_name") or ""
            if isinstance(target_val, list):
                target_val = " ".join(target_val)
            table.add_row(str(i + 1), m.get("name", ""), m.get("kind", ""), status, str(target_val))
        console.print(table)

        console.print(
            "[cyan]t<#>[/cyan] toggle, [cyan]a[/cyan]dd builtin, [cyan]c[/cyan]ustom, "
            "[cyan]r<#>[/cyan] remove, [cyan]q[/cyan]uit"
        )
        action = Prompt.ask("Action", default="q").strip().lower()
        if action in ("q", ""):
            return

        if action.startswith("t") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(rows):
                name = rows[idx].get("name")
                enable = not rows[idx].get("enabled")
                path = f"/api/mcps/{name}/{'enable' if enable else 'disable'}"
                await client.rest_post(path, {})
                console.print(f"[green]{name} {'enabled' if enable else 'disabled'}. Live on next message.[/green]")

        elif action == "a":
            builtin = Prompt.ask("Builtin name (e.g. vault, shell, web-search)").strip()
            if not builtin:
                console.print("[red]builtin name is required[/red]")
                continue
            try:
                await client.rest_post(
                    "/api/mcps",
                    {"name": builtin, "builtin_name": builtin, "enabled": True},
                )
                console.print("[green]Added. Live on next message.[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif action == "c":
            name = Prompt.ask("MCP name").strip()
            cmd = Prompt.ask("Command (space-separated argv, blank if URL)").strip()
            url = Prompt.ask("URL (blank if command)").strip() if not cmd else ""
            if not name or not (cmd or url):
                console.print("[red]Name and (command or URL) required[/red]")
                continue
            entry: dict = {"name": name, "enabled": True}
            if cmd:
                entry["command"] = cmd.split()
            if url:
                entry["url"] = url
            try:
                await client.rest_post("/api/mcps", entry)
                console.print("[green]Added. Live on next message.[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif action.startswith("r") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(rows):
                target = rows[idx].get("name")
                if Confirm.ask(f"Remove {target!r}?", default=False):
                    try:
                        await client.rest_delete(f"/api/mcps/{target}")
                        console.print("[green]Removed.[/green]")
                    except Exception as e:
                        console.print(f"[red]{e}[/red]")


# ── Models ────────────────────────────────────────────────────────────────

async def _models_menu(client: GatewayClient):
    """Model catalog editor — DB-backed + provider-discovery add flow.

    Reads the ``models`` table via /api/models. Each row is shown with
    its surrogate id so toggle/remove operations map unambiguously to
    the DB.
    """
    while True:
        db_models = (await client.rest_get("/api/models")).get("models", []) or []

        table = Table(title=f"Configured Models ({len(db_models)})")
        table.add_column("#", width=3)
        table.add_column("id", width=4, style="dim")
        table.add_column("Framework", style="dim")
        table.add_column("Provider", style="dim")
        table.add_column("Model", style="cyan")
        table.add_column("Status")
        table.add_column("Router")
        table.add_column("Tier hint", style="dim")
        table.add_column("Cost (in/out $/M)", justify="right")
        for i, m in enumerate(db_models):
            status = "[green]enabled[/green]" if m.get("enabled") else "[red]disabled[/red]"
            router = "[magenta]yes[/magenta]" if m.get("is_classifier") else ""
            fw = str(m.get("framework", "agno"))
            in_c = m.get("input_cost_per_million")
            out_c = m.get("output_cost_per_million")
            cost = f"{in_c or '-'} / {out_c or '-'}"
            tier = str(m.get("tier_hint") or "")
            if len(tier) > 32:
                tier = tier[:31] + "…"
            model_id = str(m.get("model", ""))
            display = str(m.get("display_name") or "").strip()
            # Show the friendly name when set, with the bare model id as a
            # dim subtitle so the surrogate is still visible at a glance.
            if display:
                model_cell = f"{display}\n[dim]{model_id}[/dim]"
            else:
                model_cell = model_id
            table.add_row(
                str(i + 1), str(m.get("id", "")), fw,
                str(m.get("provider_name", "")),
                model_cell, status, router, tier, cost,
            )
        console.print(table)

        console.print(
            "\n[cyan]a[/cyan]dd, [cyan]t<#>[/cyan] toggle, [cyan]c<#>[/cyan] set as router/classifier, "
            "[cyan]e<#>[/cyan] edit hints + name, [cyan]r<#>[/cyan] remove, [cyan]p<#>[/cyan] pin to session, "
            "[cyan]u[/cyan]npin session, [cyan]q[/cyan]uit"
        )
        action = Prompt.ask("Action", default="q").strip().lower()
        if action in ("q", ""):
            return

        if action == "a":
            # Pick a provider row by id (unambiguous when the same
            # vendor is registered under both frameworks).
            provs = (await client.rest_get("/api/providers")).get("providers", []) or []
            if not provs:
                console.print("[yellow]No providers configured. Add one via /providers first.[/yellow]")
                continue
            ptable = Table(title="Providers")
            ptable.add_column("#", width=3)
            ptable.add_column("id", width=4, style="dim")
            ptable.add_column("Name", style="cyan")
            ptable.add_column("Framework", style="dim")
            for i, p in enumerate(provs):
                ptable.add_row(
                    str(i + 1), str(p.get("id", "")),
                    p.get("name", ""), p.get("framework", ""),
                )
            console.print(ptable)
            pick = Prompt.ask("Pick provider # (or q to cancel)", default="q").strip().lower()
            if pick == "q" or not pick.isdigit():
                continue
            p_idx = int(pick) - 1
            if not (0 <= p_idx < len(provs)):
                continue
            provider_row = provs[p_idx]
            try:
                avail = (await client.rest_get(
                    f"/api/models/available?provider_id={provider_row['id']}"
                )).get("models", []) or []
            except Exception as e:
                console.print(f"[red]{e}[/red]")
                continue
            if not avail:
                console.print(f"[yellow]No models available from {provider_row['name']}.[/yellow]")
                continue
            atable = Table(title=f"Available from {provider_row['name']} ({provider_row['framework']})")
            atable.add_column("#", width=3)
            atable.add_column("Model", style="cyan")
            atable.add_column("Display", style="dim")
            atable.add_column("Added?")
            for i, m in enumerate(avail):
                atable.add_row(
                    str(i + 1), str(m.get("id", "")),
                    str(m.get("display_name", "")),
                    "[green]yes[/green]" if m.get("added") else "",
                )
            console.print(atable)
            pick = Prompt.ask("Pick model # (or q to cancel)", default="q").strip().lower()
            if pick == "q" or not pick.isdigit():
                continue
            idx = int(pick) - 1
            if not (0 <= idx < len(avail)):
                continue
            picked = avail[idx]
            tier_hint = Prompt.ask("tier hint (blank to skip)", default="").strip()
            name = Prompt.ask("name (blank for default)", default="").strip()
            payload = {
                "provider_id": provider_row["id"],
                "model": picked.get("id"),
                "display_name": picked.get("display_name"),
            }
            if tier_hint:
                payload["tier_hint"] = tier_hint
            if name:
                payload["display_name"] = name
            try:
                await client.rest_post("/api/models", payload)
                console.print("[green]Added. Live on next message.[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif action.startswith("t") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(db_models):
                m = db_models[idx]
                path = f"/api/models/{m['id']}/{'disable' if m.get('enabled') else 'enable'}"
                try:
                    await client.rest_post(path, {})
                    console.print("[green]Toggled. Live on next message.[/green]")
                except Exception as e:
                    console.print(f"[red]{e}[/red]")

        elif action.startswith("c") and action[1:].isdigit():
            # Toggle the is_classifier flag on this row only. Multiple
            # rows may carry the flag — the router picks the first
            # flagged entry in catalog order each turn, so flagged
            # rows form a "classifier pool". PUT /api/models/{id} is
            # a narrow update that never touches other rows.
            idx = int(action[1:]) - 1
            if 0 <= idx < len(db_models):
                m = db_models[idx]
                new_flag = not bool(m.get("is_classifier"))
                try:
                    await client.rest_put(
                        f"/api/models/{m['id']}",
                        {"is_classifier": new_flag},
                    )
                    label = "set as router" if new_flag else "cleared router flag"
                    console.print(f"[green]{label}. Live on next message.[/green]")
                except Exception as e:
                    console.print(f"[red]{e}[/red]")

        elif action.startswith("e") and action[1:].isdigit():
            # Edit tier_hint / display_name. The tier_hint is what the leader
            # uses to delegate to specialist sub-agents; display_name is the
            # friendly label shown in the catalog. Blank input explicitly
            # clears either field (sent as null).
            idx = int(action[1:]) - 1
            if 0 <= idx < len(db_models):
                m = db_models[idx]
                new_tier = Prompt.ask(
                    "tier hint (blank to clear)",
                    default=m.get("tier_hint") or "",
                ).strip()
                new_name = Prompt.ask(
                    "name (blank to clear)",
                    default=m.get("display_name") or "",
                ).strip()
                payload = {
                    "tier_hint": new_tier or None,
                    "display_name": new_name or None,
                }
                try:
                    await client.rest_put(f"/api/models/{m['id']}", payload)
                    console.print("[green]Updated. Live on next message.[/green]")
                except Exception as e:
                    console.print(f"[red]{e}[/red]")

        elif action.startswith("r") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(db_models):
                m = db_models[idx]
                if Confirm.ask(f"Remove {m['runtime_id']!r}?", default=False):
                    try:
                        await client.rest_delete(f"/api/models/{m['id']}")
                        console.print("[green]Removed.[/green]")
                    except Exception as e:
                        console.print(f"[red]{e}[/red]")

        elif action.startswith("p") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if not (0 <= idx < len(db_models)):
                continue
            session = Prompt.ask("Session ID (blank for current active session)").strip()
            if not session:
                console.print("[yellow]Session ID required (run /sessions to list).[/yellow]")
                continue
            m = db_models[idx]
            try:
                await client.rest_put(
                    f"/api/sessions/{session}/model",
                    {"runtime_id": m["runtime_id"]},
                )
                console.print(f"[green]Pinned {session} → {m['runtime_id']}.[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red] [dim](cross-framework pins are rejected — unpin first)[/dim]")

        elif action == "u":
            session = Prompt.ask("Session ID to unpin").strip()
            if not session:
                continue
            try:
                await client.rest_delete(f"/api/sessions/{session}/model")
                console.print(f"[green]Unpinned {session}.[/green]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")


# ── Providers ─────────────────────────────────────────────────────────────

async def _providers_menu(client: GatewayClient):
    while True:
        data = await client.rest_get("/api/providers")
        provs = data.get("providers", []) or []

        table = Table(title=f"Providers ({len(provs)})")
        table.add_column("#", width=3)
        table.add_column("id", width=4, style="dim")
        table.add_column("Name", style="cyan")
        table.add_column("Framework", style="dim")
        table.add_column("API Key")
        table.add_column("Base URL", style="dim")
        for i, info in enumerate(provs):
            table.add_row(
                str(i + 1),
                str(info.get("id", "")),
                info.get("name", ""),
                info.get("framework", ""),
                info.get("api_key_display", ""),
                info.get("base_url", "") or "",
            )
        console.print(table)

        console.print("[cyan]a[/cyan]dd, [cyan]t<#>[/cyan] test, [cyan]r<#>[/cyan] remove, [cyan]q[/cyan]uit")
        action = Prompt.ask("Action", default="q").strip().lower()
        if action in ("q", ""):
            return

        if action == "a":
            name = Prompt.ask("Provider name (e.g. openai, anthropic, zai)").strip()
            framework = Prompt.ask(
                "Framework",
                choices=["agno"],
                default="agno",
            )
            api_key = Prompt.ask("API key").strip()
            base_url = Prompt.ask("Base URL (optional)").strip()
            payload: dict[str, Any] = {"name": name, "framework": framework}
            if api_key:
                payload["api_key"] = api_key
            if base_url:
                payload["base_url"] = base_url
            try:
                res = await client.rest_post("/api/providers", payload)
                if res.get("ok"):
                    console.print("[green]Added. Live on next message.[/green]")
                else:
                    console.print(f"[red]Failed: {res}[/red]")
            except Exception as e:
                console.print(f"[red]{e}[/red]")

        elif action.startswith("t") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(provs):
                prov = provs[idx]
                console.print(f"[dim]Testing {prov.get('name')} ({prov.get('framework')})...[/dim]")
                res = await client.rest_post(f"/api/providers/{prov['id']}/test", {})
                if res.get("ok"):
                    console.print(f"[green]✓ {res.get('model', '?')}: {res.get('response', '')[:80]}[/green]")
                else:
                    console.print(f"[red]✗ {res.get('error', 'failed')}[/red]")

        elif action.startswith("r") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(provs):
                prov = provs[idx]
                label = f"{prov.get('name')} ({prov.get('framework')})"
                if Confirm.ask(f"Remove provider {label} (cascade-deletes its models)?", default=False):
                    res = await client.rest_delete(f"/api/providers/{prov['id']}")
                    if res.get("ok"):
                        purged = res.get("models_purged", 0)
                        console.print(f"[green]Removed ({purged} model(s) purged).[/green]")
                    else:
                        console.print(f"[red]Failed: {res}[/red]")


# ── Usage ─────────────────────────────────────────────────────────────────

async def _usage_menu(client: GatewayClient):
    summary = await client.rest_get("/api/usage")
    spend = summary.get("monthly_spend", 0) or 0
    budget = summary.get("monthly_budget", 0) or 0
    remaining = summary.get("remaining", 0) or 0
    console.print(Panel(
        f"[bold]Monthly spend:[/bold] ${spend:.2f}\n"
        f"[bold]Budget:[/bold] ${budget:.2f}\n"
        f"[bold]Remaining:[/bold] ${remaining:.2f}",
        title="Usage", border_style="green" if remaining > 0 else "yellow",
    ))

    by_model = summary.get("by_model", {}) or {}
    if by_model:
        table = Table(title="By model")
        table.add_column("Model", style="cyan")
        table.add_column("$ spent", justify="right")
        for m, v in by_model.items():
            table.add_row(str(m), f"${float(v):.4f}" if isinstance(v, (int, float)) else str(v))
        console.print(table)

    show_daily = Confirm.ask("Show 7-day breakdown?", default=False)
    if show_daily:
        daily = await client.rest_get("/api/usage/daily?days=7")
        entries = daily.get("entries", []) or []
        if not entries:
            console.print("[dim]No usage entries.[/dim]")
            return
        table = Table(title="Daily usage (last 7 days)")
        table.add_column("Date", style="cyan")
        table.add_column("Model")
        table.add_column("Requests", justify="right")
        table.add_column("Cost", justify="right")
        for e in entries:
            table.add_row(
                str(e.get("date", "")),
                str(e.get("model", "")),
                str(e.get("requests", "")),
                f"${float(e.get('cost', 0)):.4f}",
            )
        console.print(table)


# ── File upload ──────────────────────────────────────────────────────────

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tiff"}


def _kind_for(path: Path) -> str:
    return "image" if path.suffix.lower() in IMAGE_SUFFIXES else "file"


def _icon_for(kind: str) -> str:
    return "🖼" if kind == "image" else "📎"


def _render_attachments(items: list[tuple[str, str]]) -> None:
    """Print a pretty echo of attachments about to be sent (list of (kind, filename))."""
    chips = "  ".join(f"[cyan]{_icon_for(k)} {name}[/cyan]" for k, name in items)
    label = "Attached" if len(items) == 1 else f"Attached ({len(items)})"
    console.print(Panel(chips, title=label, border_style="cyan", padding=(0, 1)))


async def _send_files(client: GatewayClient, filepaths: list[str], session_id: str):
    """Upload one or more files and send them to the agent in a single message."""
    import aiohttp

    resolved: list[Path] = []
    for fp in filepaths:
        p = Path(fp).expanduser()
        if not p.exists():
            console.print(f"[red]File not found: {fp}[/red]")
            continue
        resolved.append(p)

    if not resolved:
        return

    uploaded: list[tuple[str, str, str]] = []  # (kind, filename, remote_path)
    for p in resolved:
        console.print(f"[dim]Uploading {p.name}...[/dim]")
        try:
            form = aiohttp.FormData()
            form.add_field("file", open(p, "rb"), filename=p.name)
            async with client._session.post(f"{client.base_url}/api/upload", data=form) as resp:
                result = await resp.json()
            uploaded.append((_kind_for(p), result["filename"], result["path"]))
        except Exception as e:
            console.print(f"[red]Upload failed for {p.name}: {e}[/red]")

    if not uploaded:
        return

    # Pretty echo of what's being sent
    _render_attachments([(k, name) for k, name, _ in uploaded])

    lines = [f"- {k}: {name} — local path: {path}" for k, name, path in uploaded]
    noun = "a file" if len(uploaded) == 1 else f"{len(uploaded)} files"
    inspect = "it" if len(uploaded) == 1 else "them"
    msg = (
        f"The user attached {noun}:\n"
        + "\n".join(lines)
        + f"\nUse the Read tool to inspect {inspect}."
    )

    await _send_message_with_indicator(client, msg, session_id)


def main():
    cli()


if __name__ == "__main__":
    main()
