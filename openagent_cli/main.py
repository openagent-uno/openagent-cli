"""OpenAgent CLI — interactive client for any OpenAgent Gateway.

Usage:
    openagent-cli connect localhost:8765
    openagent-cli connect localhost:8765 --token mysecret
    openagent-cli connect user@vps:8765 --ssh
"""

from __future__ import annotations

import asyncio
import sys

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from openagent_cli.client import GatewayClient

console = Console()


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
    console.print("[dim]Commands: /new /stop /status /quit /vault /config /tasks[/dim]\n")

    session_id = "cli-default"
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

        # Built-in CLI commands
        if text in ("/quit", "/exit", "/q"):
            break

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

        if text == "/vault":
            await _vault_menu(client)
            continue

        if text == "/config":
            await _config_menu(client)
            continue

        if text == "/tasks":
            await _tasks_menu(client)
            continue

        # Gateway commands
        if text.startswith("/"):
            cmd = text[1:].split()[0]
            result = await client.send_command(cmd)
            console.print(f"[dim]{result}[/dim]")
            continue

        # Chat message
        console.print("[dim]⏳ Thinking...[/dim]", end="")

        async def on_status(status: str):
            console.print(f"\r[dim]⏳ {status}[/dim]", end="")

        response = await client.send_message(text, active, on_status=on_status)
        console.print("\r" + " " * 60 + "\r", end="")

        resp_text = response.get("text", "")
        if response.get("type") == "error":
            console.print(f"[red]Error: {resp_text}[/red]")
        else:
            console.print(Markdown(resp_text))
        console.print()

    await client.disconnect()
    console.print("[dim]Disconnected.[/dim]")


async def _vault_menu(client: GatewayClient):
    """Interactive vault browser."""
    data = await client.rest_get("/api/vault/notes")
    notes = data.get("notes", [])

    table = Table(title=f"Vault ({len(notes)} notes)")
    table.add_column("#", width=3)
    table.add_column("Title")
    table.add_column("Tags", style="dim")
    for i, n in enumerate(notes):
        tags = ", ".join(n.get("tags", [])[:3])
        table.add_row(str(i + 1), n.get("title", n["path"]), tags)
    console.print(table)

    choice = Prompt.ask("Read note # (or 'q' to go back)")
    if choice.lower() in ("q", ""):
        return
    try:
        idx = int(choice) - 1
        note = notes[idx]
        data = await client.rest_get(f"/api/vault/notes/{note['path']}")
        console.print(Panel(Markdown(data.get("body", data.get("content", ""))), title=note["path"]))
    except (ValueError, IndexError):
        console.print("[red]Invalid selection[/red]")


async def _config_menu(client: GatewayClient):
    """Show current config summary."""
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


async def _tasks_menu(client: GatewayClient):
    """Show scheduled tasks."""
    cfg = await client.rest_get("/api/config")
    tasks = cfg.get("scheduler", {}).get("tasks", [])

    if not tasks:
        console.print("[dim]No scheduled tasks.[/dim]")
        return

    table = Table(title=f"Scheduled Tasks ({len(tasks)})")
    table.add_column("Name", style="cyan")
    table.add_column("Cron")
    table.add_column("Prompt", max_width=50)
    for t in tasks:
        console.print()
        table.add_row(t.get("name", "?"), t.get("cron", "?"), (t.get("prompt", ""))[:50])
    console.print(table)


def main():
    cli()


if __name__ == "__main__":
    main()
