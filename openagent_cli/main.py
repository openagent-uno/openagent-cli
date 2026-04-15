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

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from openagent_cli.client import GatewayClient

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

def _render_response(response: dict) -> None:
    resp_text = response.get("text", "")
    if response.get("type") == "error":
        console.print(f"[red]Error: {resp_text}[/red]")
    else:
        console.print(Markdown(resp_text))
        model = response.get("model")
        if model:
            console.print(f"[dim]Model: {model}[/dim]")
    console.print()


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
        ("/file <path>", "Upload a file/image to the agent"),
        ("/stop", "Cancel the current operation"),
        ("/status", "Show agent status & queue"),
        ("/clear", "Clear the message queue"),
        ("/usage", "Show monthly spend & budget"),
        ("/vault", "Browse, search, edit notes"),
        ("/mcps", "List & toggle MCP servers"),
        ("/models", "List providers & switch active model"),
        ("/providers", "List, test, add providers"),
        ("/settings", "Edit identity, prompt, channels, dream, auto-update"),
        ("/tasks", "Manage scheduled tasks"),
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
@click.argument("host_port", default="localhost:8765")
@click.option("--token", "-t", default=None, help="Gateway auth token")
def connect(host_port: str, token: str | None):
    """Connect to an OpenAgent Gateway and start interactive session."""
    url = f"ws://{host_port}/ws"
    asyncio.run(_interactive(url, token))


async def _interactive(url: str, token: str | None):
    client = GatewayClient(url, token)
    try:
        await client.connect()
    except Exception as e:
        console.print(f"[red]Connection failed:[/red] {e}")
        return

    console.print(Panel(
        f"[bold]{client.agent_name}[/bold] v{client.agent_version}\n"
        f"Gateway: {url}",
        title="Connected", border_style="green",
    ))
    console.print("[dim]Type /help for commands.[/dim]\n")

    sessions = {"cli-default": "Default"}
    active = "cli-default"

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
            for sid, name in sessions.items():
                marker = "→ " if sid == active else "  "
                console.print(f"{marker}[cyan]{sid[-8:]}[/cyan] {name}")
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

        if text == "/mcps":
            await _mcps_menu(client)
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

        # ── Gateway pass-through commands ──
        # /clear, /restart, /update, /status, /stop, /queue, /reset
        if text.startswith("/"):
            cmd = text[1:].split()[0]
            try:
                result = await client.send_command(cmd)
                console.print(f"[dim]{result}[/dim]")
            except Exception as e:
                console.print(f"[red]Command failed: {e}[/red]")
            continue

        # ── Chat message ──
        console.print("[dim]⏳ Thinking...[/dim]", end="")

        async def on_status(status: str):
            line = format_tool_status(status)
            console.print(f"\r[dim]⏳ {line}[/dim]" + " " * 20, end="")

        response = await client.send_message(text, active, on_status=on_status)
        console.print("\r" + " " * 80 + "\r", end="")
        _render_response(response)

    await client.disconnect()
    console.print("[dim]Disconnected.[/dim]")


# ── Vault menu ───────────────────────────────────────────────────────────

async def _vault_menu(client: GatewayClient):
    """Interactive vault browser with search and edit."""
    while True:
        console.print("\n[bold]Vault[/bold] — [cyan]l[/cyan]ist, [cyan]s[/cyan]earch, [cyan]e[/cyan]dit, [cyan]n[/cyan]ew, [cyan]d[/cyan]elete, [cyan]q[/cyan]uit")
        action = Prompt.ask("Action", choices=["l", "s", "e", "n", "d", "q"], default="l")
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
            if res.get("ok"):
                console.print(f"[green]Created {path}[/green]")
            else:
                console.print(f"[red]Failed: {res}[/red]")
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
    if res.get("ok"):
        console.print(f"[green]Saved {path}[/green]")
    else:
        console.print(f"[red]Save failed: {res}[/red]")


# ── Config / settings ────────────────────────────────────────────────────

async def _config_menu(client: GatewayClient):
    cfg = await client.rest_get("/api/config")
    table = Table(title="Configuration")
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_row("name", str(cfg.get("name", "")))
    table.add_row("model.provider", str(cfg.get("model", {}).get("provider", "")))
    table.add_row("model.model_id", str(cfg.get("model", {}).get("model_id", "")))
    table.add_row("mcp_defaults", str(cfg.get("mcp_defaults", True)))
    table.add_row("custom MCPs", str(len(cfg.get("mcp", []))))
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
    console.print("\n[bold]Channels[/bold]: g(ateway), t(elegram), d(iscord), w(hatsApp)")
    which = Prompt.ask("Edit", choices=["g", "t", "d", "w", "q"], default="q")
    if which == "q":
        return

    if which == "g":
        ws = channels.get("websocket", {}) or {}
        host = Prompt.ask("Host", default=ws.get("host", "0.0.0.0"))
        port = int(Prompt.ask("Port", default=str(ws.get("port", 8765))))
        token = Prompt.ask("Token (blank for env var)", default=ws.get("token", ""))
        new_channels = dict(channels)
        new_channels["websocket"] = {"host": host, "port": port, "token": token}
        await client.rest_patch("/api/config/channels", new_channels)

    elif which == "t":
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


# ── Tasks (cron) ─────────────────────────────────────────────────────────

async def _tasks_menu(client: GatewayClient):
    while True:
        cfg = await client.rest_get("/api/config")
        sched = cfg.get("scheduler", {}) or {}
        tasks = sched.get("tasks", []) or []

        table = Table(title=f"Scheduled Tasks ({len(tasks)})  enabled={sched.get('enabled', False)}")
        table.add_column("#", width=3)
        table.add_column("Name", style="cyan")
        table.add_column("Cron")
        table.add_column("Prompt", max_width=50)
        for i, t in enumerate(tasks):
            table.add_row(str(i + 1), t.get("name", "?"), t.get("cron", "?"), (t.get("prompt", ""))[:50])
        console.print(table)
        console.print("[cyan]a[/cyan]dd, [cyan]e[/cyan]dit #, [cyan]d[/cyan]elete #, [cyan]t[/cyan]oggle scheduler, [cyan]q[/cyan]uit")
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
            tasks.append({"name": name, "cron": cron, "prompt": prompt})
            await client.rest_patch("/api/config/scheduler", {"enabled": sched.get("enabled", True), "tasks": tasks})
            console.print("[green]Added. Restart required.[/green]")

        elif action.startswith("e") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(tasks):
                t = tasks[idx]
                t["name"] = Prompt.ask("Name", default=t.get("name", ""))
                t["cron"] = Prompt.ask("Cron", default=t.get("cron", ""))
                t["prompt"] = Prompt.ask("Prompt", default=t.get("prompt", ""))
                await client.rest_patch("/api/config/scheduler", {"enabled": sched.get("enabled", True), "tasks": tasks})
                console.print("[green]Saved. Restart required.[/green]")

        elif action.startswith("d") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(tasks):
                if Confirm.ask(f"Delete '{tasks[idx].get('name')}'?", default=False):
                    del tasks[idx]
                    await client.rest_patch("/api/config/scheduler", {"enabled": sched.get("enabled", True), "tasks": tasks})
                    console.print("[green]Deleted. Restart required.[/green]")

        elif action == "t":
            new_enabled = not sched.get("enabled", False)
            await client.rest_patch("/api/config/scheduler", {"enabled": new_enabled, "tasks": tasks})
            console.print(f"[green]Scheduler {'enabled' if new_enabled else 'disabled'}. Restart required.[/green]")


# ── MCPs ─────────────────────────────────────────────────────────────────

DEFAULT_MCPS = [
    "vault", "filesystem", "editor", "web-search", "shell",
    "computer-control", "chrome-devtools", "messaging", "scheduler",
]


async def _mcps_menu(client: GatewayClient):
    while True:
        cfg = await client.rest_get("/api/config")
        disabled = set(cfg.get("mcp_disable", []) or [])
        custom = cfg.get("mcp", []) or []

        table = Table(title="Default MCPs")
        table.add_column("#", width=3)
        table.add_column("Name", style="cyan")
        table.add_column("Status")
        for i, m in enumerate(DEFAULT_MCPS):
            status = "[red]disabled[/red]" if m in disabled else "[green]enabled[/green]"
            table.add_row(str(i + 1), m, status)
        console.print(table)

        if custom:
            ctable = Table(title=f"Custom MCPs ({len(custom)})")
            ctable.add_column("#", width=3)
            ctable.add_column("Name", style="cyan")
            ctable.add_column("Command/URL", style="dim")
            for i, m in enumerate(custom):
                key = m.get("command") or m.get("url", "")
                ctable.add_row(str(i + 1), m.get("name", ""), key)
            console.print(ctable)

        console.print("[cyan]t<#>[/cyan] toggle default, [cyan]a[/cyan]dd custom, [cyan]r<#>[/cyan] remove custom, [cyan]q[/cyan]uit")
        action = Prompt.ask("Action", default="q").strip().lower()
        if action in ("q", ""):
            return

        if action.startswith("t") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(DEFAULT_MCPS):
                name = DEFAULT_MCPS[idx]
                if name in disabled:
                    disabled.remove(name)
                else:
                    disabled.add(name)
                await client.rest_patch("/api/config/mcp_disable", sorted(disabled))
                console.print(f"[green]{name} {'disabled' if name in disabled else 'enabled'}. Restart required.[/green]")

        elif action == "a":
            name = Prompt.ask("MCP name").strip()
            cmd = Prompt.ask("Command (blank if URL)").strip()
            url = Prompt.ask("URL (blank if command)").strip() if not cmd else ""
            if not name or not (cmd or url):
                console.print("[red]Name and (command or URL) required[/red]")
                continue
            entry = {"name": name}
            if cmd:
                entry["command"] = cmd
            if url:
                entry["url"] = url
            custom.append(entry)
            await client.rest_patch("/api/config/mcp", custom)
            console.print("[green]Added. Restart required.[/green]")

        elif action.startswith("r") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(custom):
                if Confirm.ask(f"Remove '{custom[idx].get('name')}'?", default=False):
                    del custom[idx]
                    await client.rest_patch("/api/config/mcp", custom)
                    console.print("[green]Removed. Restart required.[/green]")


# ── Models ────────────────────────────────────────────────────────────────

async def _models_menu(client: GatewayClient):
    data = await client.rest_get("/api/models")
    providers = data.get("models", {}) or {}
    active = data.get("active", {}) or {}

    table = Table(title="Configured Providers")
    table.add_column("Provider", style="cyan")
    table.add_column("API Key")
    table.add_column("Base URL", style="dim")
    table.add_column("Models", max_width=40)
    for name, info in providers.items():
        models = info.get("models", []) or []
        models_str = ", ".join(models[:5]) + ("..." if len(models) > 5 else "")
        table.add_row(
            name,
            info.get("api_key_display", ""),
            info.get("base_url", "") or "",
            models_str,
        )
    console.print(table)

    if active:
        console.print(f"\n[bold]Active model:[/bold] [cyan]{active.get('provider', '')}[/cyan] / "
                      f"[cyan]{active.get('model_id', '')}[/cyan]")

    console.print("\n[cyan]s[/cyan]witch active, [cyan]c[/cyan]atalog, [cyan]q[/cyan]uit")
    action = Prompt.ask("Action", choices=["s", "c", "q"], default="q")
    if action == "q":
        return

    if action == "c":
        cat = await client.rest_get("/api/models/catalog")
        models = cat.get("models", [])
        ctable = Table(title=f"Model Catalog ({len(models)})")
        ctable.add_column("Provider", style="cyan")
        ctable.add_column("Model")
        ctable.add_column("In $/M", justify="right")
        ctable.add_column("Out $/M", justify="right")
        for m in models:
            ctable.add_row(
                str(m.get("provider", "")),
                str(m.get("model_id", "")),
                f"{m.get('input_cost_per_million', '')}",
                f"{m.get('output_cost_per_million', '')}",
            )
        console.print(ctable)

    elif action == "s":
        provider = Prompt.ask("Provider name").strip()
        model_id = Prompt.ask("Model ID").strip()
        if not provider or not model_id:
            return
        await client.rest_put("/api/models/active", {"provider": provider, "model_id": model_id})
        console.print(f"[green]Active model set to {provider}/{model_id}.[/green]")


# ── Providers ─────────────────────────────────────────────────────────────

async def _providers_menu(client: GatewayClient):
    while True:
        data = await client.rest_get("/api/providers")
        providers = data.get("providers", {}) or {}

        table = Table(title=f"Providers ({len(providers)})")
        table.add_column("#", width=3)
        table.add_column("Name", style="cyan")
        table.add_column("API Key")
        table.add_column("Base URL", style="dim")
        names = list(providers.keys())
        for i, name in enumerate(names):
            info = providers[name]
            table.add_row(str(i + 1), name, info.get("api_key_display", ""), info.get("base_url", "") or "")
        console.print(table)

        console.print("[cyan]a[/cyan]dd, [cyan]t<#>[/cyan] test, [cyan]r<#>[/cyan] remove, [cyan]q[/cyan]uit")
        action = Prompt.ask("Action", default="q").strip().lower()
        if action in ("q", ""):
            return

        if action == "a":
            name = Prompt.ask("Provider name").strip()
            api_key = Prompt.ask("API key (blank for env var)").strip()
            base_url = Prompt.ask("Base URL (optional)").strip()
            payload = {"name": name}
            if api_key:
                payload["api_key"] = api_key
            if base_url:
                payload["base_url"] = base_url
            res = await client.rest_post("/api/models", payload)
            if res.get("ok"):
                console.print("[green]Added.[/green]")
            else:
                console.print(f"[red]Failed: {res}[/red]")

        elif action.startswith("t") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(names):
                name = names[idx]
                console.print(f"[dim]Testing {name}...[/dim]")
                res = await client.rest_post(f"/api/models/{name}/test", {})
                if res.get("ok"):
                    console.print(f"[green]✓ {res.get('model', '?')}: {res.get('response', '')[:80]}[/green]")
                else:
                    console.print(f"[red]✗ {res.get('error', 'failed')}[/red]")

        elif action.startswith("r") and action[1:].isdigit():
            idx = int(action[1:]) - 1
            if 0 <= idx < len(names):
                name = names[idx]
                if Confirm.ask(f"Remove provider '{name}'?", default=False):
                    res = await client.rest_delete(f"/api/models/{name}")
                    if res.get("ok"):
                        console.print("[green]Removed.[/green]")
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

async def _send_file(client: GatewayClient, filepath: str, session_id: str):
    """Upload a file and send it to the agent."""
    import aiohttp

    p = Path(filepath).expanduser()
    if not p.exists():
        console.print(f"[red]File not found: {filepath}[/red]")
        return

    console.print(f"[dim]Uploading {p.name}...[/dim]")
    try:
        form = aiohttp.FormData()
        form.add_field('file', open(p, 'rb'), filename=p.name)
        async with client._session.post(f"{client.base_url}/api/upload", data=form) as resp:
            result = await resp.json()
        remote_path = result["path"]
        filename = result["filename"]
    except Exception as e:
        console.print(f"[red]Upload failed: {e}[/red]")
        return

    kind = "image" if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp") else "file"
    msg = f"The user attached a file:\n- {kind}: {filename} — local path: {remote_path}\nUse the Read tool to inspect it."

    console.print(f"[dim]📎 {filename} uploaded. Sending to agent...[/dim]")

    async def on_status(s):
        line = format_tool_status(s)
        console.print(f"\r[dim]⏳ {line}[/dim]" + " " * 20, end="")

    response = await client.send_message(msg, session_id, on_status=on_status)
    console.print("\r" + " " * 80 + "\r", end="")
    _render_response(response)


def main():
    cli()


if __name__ == "__main__":
    main()
