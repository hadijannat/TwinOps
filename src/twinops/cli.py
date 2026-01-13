"""TwinOps CLI - Task management and administration."""

import asyncio
import json
import sys
from collections.abc import Callable, Coroutine
from typing import Any, ParamSpec, TypeVar

import click
from rich.console import Console
from rich.table import Table

from twinops.agent.policy_signing import generate_keypair, sign_policy, verify_policy_signature
from twinops.agent.safety import AuditLogger
from twinops.agent.twin_client import TwinClient, TwinClientError
from twinops.common.settings import Settings

console = Console()

P = ParamSpec("P")
R = TypeVar("R")


def async_command(f: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, R]:
    """Decorator to run async commands."""

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return asyncio.run(f(*args, **kwargs))

    return wrapper


@click.group()
@click.option(
    "--base-url",
    default="http://localhost:8081",
    help="AAS repository base URL",
)
@click.pass_context
def cli(ctx: click.Context, base_url: str) -> None:
    """TwinOps CLI - Manage AI agent tasks and policies."""
    ctx.ensure_object(dict)
    ctx.obj["base_url"] = base_url


# === Task Management ===


@cli.command("list-tasks")
@click.option(
    "--submodel-id", default="urn:example:submodel:control", help="Submodel ID containing tasks"
)
@click.option("--property-path", default="TasksJson", help="Path to TasksJson property")
@click.pass_context
@async_command
async def list_tasks(ctx: click.Context, submodel_id: str, property_path: str) -> None:
    """List pending approval tasks."""
    settings = Settings(twin_base_url=ctx.obj["base_url"])

    async with TwinClient(settings) as client:
        try:
            tasks = await client.get_tasks(submodel_id, property_path)
        except TwinClientError as e:
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    if not tasks:
        console.print("[yellow]No pending tasks[/yellow]")
        return

    table = Table(title="Pending Tasks")
    table.add_column("Task ID", style="cyan")
    table.add_column("Tool", style="green")
    table.add_column("Risk", style="yellow")
    table.add_column("Status", style="magenta")
    table.add_column("Requested By")
    table.add_column("Created")

    for task in tasks:
        import time

        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(task.get("created_at", 0)))
        table.add_row(
            task.get("task_id", ""),
            task.get("tool", ""),
            task.get("risk", ""),
            task.get("status", ""),
            ", ".join(task.get("requested_by_roles", [])),
            created,
        )

    console.print(table)


@cli.command("approve")
@click.option("--task-id", required=True, help="Task ID to approve")
@click.option("--submodel-id", default="urn:example:submodel:control", help="Submodel ID")
@click.option("--property-path", default="TasksJson", help="Path to TasksJson")
@click.pass_context
@async_command
async def approve_task(
    ctx: click.Context, task_id: str, submodel_id: str, property_path: str
) -> None:
    """Approve a pending task."""
    settings = Settings(twin_base_url=ctx.obj["base_url"])

    async with TwinClient(settings) as client:
        try:
            success = await client.update_task_status(
                submodel_id,
                property_path,
                task_id,
                "Approved",
            )
        except TwinClientError as e:
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    if success:
        console.print(f"[green]Task {task_id} approved[/green]")
    else:
        console.print(f"[red]Task {task_id} not found[/red]")
        sys.exit(1)


@cli.command("reject")
@click.option("--task-id", required=True, help="Task ID to reject")
@click.option("--reason", default="Rejected by operator", help="Rejection reason")
@click.option("--submodel-id", default="urn:example:submodel:control", help="Submodel ID")
@click.option("--property-path", default="TasksJson", help="Path to TasksJson")
@click.pass_context
@async_command
async def reject_task(
    ctx: click.Context,
    task_id: str,
    reason: str,
    submodel_id: str,
    property_path: str,
) -> None:
    """Reject a pending task."""
    settings = Settings(twin_base_url=ctx.obj["base_url"])

    async with TwinClient(settings) as client:
        try:
            success = await client.update_task_status(
                submodel_id,
                property_path,
                task_id,
                "Rejected",
                reason=reason,
            )
        except TwinClientError as e:
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    if success:
        console.print(f"[green]Task {task_id} rejected: {reason}[/green]")
    else:
        console.print(f"[red]Task {task_id} not found[/red]")
        sys.exit(1)


# === Policy Management ===


@cli.command("generate-keypair")
@click.option("--output", "-o", default=".", help="Output directory for keys")
def generate_keys(output: str) -> None:
    """Generate Ed25519 key pair for policy signing."""
    from pathlib import Path

    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    private_pem, public_pem = generate_keypair()

    private_path = output_dir / "policy_private.pem"
    public_path = output_dir / "policy_public.pem"

    private_path.write_text(private_pem)
    public_path.write_text(public_pem)

    console.print(f"[green]Private key saved to: {private_path}[/green]")
    console.print(f"[green]Public key saved to: {public_path}[/green]")
    console.print("[yellow]Keep the private key secure![/yellow]")


@cli.command("sign-policy")
@click.option("--policy-file", "-p", required=True, help="Path to policy JSON file")
@click.option("--private-key", "-k", required=True, help="Path to private key PEM")
@click.option("--output", "-o", help="Output file (default: stdout)")
def sign_policy_cmd(policy_file: str, private_key: str, output: str | None) -> None:
    """Sign a policy file."""
    from pathlib import Path

    policy_path = Path(policy_file)
    key_path = Path(private_key)

    if not policy_path.exists():
        console.print(f"[red]Policy file not found: {policy_file}[/red]")
        sys.exit(1)

    if not key_path.exists():
        console.print(f"[red]Private key not found: {private_key}[/red]")
        sys.exit(1)

    policy_json = policy_path.read_text()
    private_pem = key_path.read_text()

    signature = sign_policy(policy_json, private_pem)

    result = {
        "policy": json.loads(policy_json),
        "signature": signature,
    }

    if output:
        Path(output).write_text(json.dumps(result, indent=2))
        console.print(f"[green]Signed policy saved to: {output}[/green]")
    else:
        console.print(json.dumps(result, indent=2))


@cli.command("verify-policy")
@click.option("--policy-json", "-p", required=True, help="Policy JSON string or file path")
@click.option("--public-key", "-k", required=True, help="Public key PEM file path")
@click.option("--signature", "-s", required=True, help="Base64 signature")
def verify_policy_cmd(policy_json: str, public_key: str, signature: str) -> None:
    """Verify a policy signature."""
    from pathlib import Path

    # Load policy JSON
    if Path(policy_json).exists():
        policy_json = Path(policy_json).read_text()

    # Load public key
    key_path = Path(public_key)
    if not key_path.exists():
        console.print(f"[red]Public key not found: {public_key}[/red]")
        sys.exit(1)
    public_pem = key_path.read_text()

    is_valid = verify_policy_signature(policy_json, public_pem, signature)

    if is_valid:
        console.print("[green]✓ Signature is valid[/green]")
    else:
        console.print("[red]✗ Signature is invalid[/red]")
        sys.exit(1)


# === Audit Management ===


@cli.command("verify-audit")
@click.option("--log-path", default="audit_logs/audit.jsonl", help="Path to audit log")
def verify_audit(log_path: str) -> None:
    """Verify audit log integrity."""
    audit = AuditLogger(log_path)
    is_valid, broken = audit.verify_chain()

    if is_valid:
        console.print("[green]✓ Audit log integrity verified[/green]")
    else:
        console.print(f"[red]✗ Audit log corrupted at lines: {broken}[/red]")
        sys.exit(1)


@cli.command("show-audit")
@click.option("--log-path", default="audit_logs/audit.jsonl", help="Path to audit log")
@click.option("--last", "-n", default=20, help="Number of entries to show")
@click.option("--filter-event", help="Filter by event type")
@click.option("--filter-tool", help="Filter by tool name")
def show_audit(log_path: str, last: int, filter_event: str | None, filter_tool: str | None) -> None:
    """Show recent audit log entries."""
    from pathlib import Path

    log_file = Path(log_path)
    if not log_file.exists():
        console.print(f"[yellow]Audit log not found: {log_path}[/yellow]")
        return

    entries = []
    with open(log_file) as f:
        for line in f:
            try:
                entry = json.loads(line)
                if filter_event and entry.get("event") != filter_event:
                    continue
                if filter_tool and entry.get("tool") != filter_tool:
                    continue
                entries.append(entry)
            except json.JSONDecodeError:
                pass

    # Show last N entries
    entries = entries[-last:]

    table = Table(title=f"Audit Log (last {len(entries)} entries)")
    table.add_column("Time", style="cyan")
    table.add_column("Event", style="green")
    table.add_column("Tool", style="yellow")
    table.add_column("Risk")
    table.add_column("Details")

    import time

    for entry in entries:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.get("ts", 0)))
        details = ""
        if entry.get("error"):
            details = f"[red]{entry['error']}[/red]"
        elif entry.get("reason"):
            details = entry["reason"]
        elif entry.get("result"):
            details = str(entry["result"])[:50]

        table.add_row(
            ts,
            entry.get("event", ""),
            entry.get("tool", "-"),
            entry.get("risk", "-"),
            details,
        )

    console.print(table)


# === Status Commands ===


@cli.command("status")
@click.pass_context
@async_command
async def show_status(ctx: click.Context) -> None:
    """Show twin and agent status."""
    settings = Settings(twin_base_url=ctx.obj["base_url"])

    async with TwinClient(settings) as client:
        try:
            shells = await client.get_all_aas()
            console.print("[green]✓ Twin connected[/green]")
            console.print(f"  AAS count: {len(shells)}")

            for shell in shells:
                console.print(f"  - {shell.get('id', 'Unknown')}")

        except TwinClientError as e:
            console.print(f"[red]✗ Twin connection failed: {e}[/red]")


def main() -> None:
    """CLI entry point."""
    cli(obj={})


if __name__ == "__main__":
    main()
