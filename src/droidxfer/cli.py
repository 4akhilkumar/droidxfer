"""Command-line interface: `droidxfer serve | devices | ls | push | pull | pair | qr | connect | ...`."""

from __future__ import annotations

import asyncio
import functools
import logging
import secrets
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable

import typer
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn, TransferSpeedColumn
from rich.table import Table

from .adb import AdbError
from .core.context import AppContext
from .core.devices import DeviceError
from .core.paths import PathError
from .core.remotefs import RemoteOpError
from .core.transfer import FINISHED, Direction, TransferJob
from .core.wireless import WirelessError

app = typer.Typer(help="Transfer files between this Mac and Android devices over ADB.", no_args_is_help=True)
console = Console()
err = Console(stderr=True)

DeviceOpt = Annotated[str | None, typer.Option("--device", "-d", help="Device id or adb serial.")]
ConflictOpt = Annotated[str | None, typer.Option("--conflict", "-c", help="rename | overwrite | skip | skip_identical")]
VerifyOpt = Annotated[bool | None, typer.Option("--verify/--no-verify", help="Compare MD5 after copying.")]
_HANDLED = (AdbError, DeviceError, PathError, RemoteOpError, WirelessError, OSError, ValueError, KeyError)


def run_async(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return asyncio.run(fn(*args, **kwargs))
        except _HANDLED as exc:
            err.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from None

    return wrapper


async def _context() -> AppContext:
    ctx = AppContext()
    await ctx.start(track=False)
    return ctx


def _pick_device(ctx: AppContext, device: str | None) -> str:
    if device:
        return ctx.devices.get(device).id
    ready = [d for d in ctx.devices.devices() if d.active()]
    if len(ready) == 1:
        return ready[0].id
    if not ready:
        raise DeviceError("no authorized device found. Connect a phone with USB debugging enabled.")
    raise DeviceError("several devices connected; choose one with --device (see `droidxfer devices`)")


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


async def _follow(ctx: AppContext, job: TransferJob, queue: asyncio.Queue) -> None:
    columns = (
        TextColumn("{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    )
    with Progress(*columns, console=console) as progress:
        task = progress.add_task(job.title, total=None)
        while job.status not in FINISHED:
            try:
                event = await asyncio.wait_for(queue.get(), 0.5)
            except asyncio.TimeoutError:
                continue
            if event["type"] != "job" or event["data"]["id"] != job.id:
                continue
            data = event["data"]
            label = data["current"] or data["status"]
            progress.update(
                task,
                total=data["total_bytes"] or None,
                completed=data["bytes_done"],
                description=f"[cyan]{data['done_files']}/{data['total_files']}[/cyan] {label[-40:]}",
            )
    d = job.to_dict()
    color = {"completed": "green", "failed": "red", "cancelled": "yellow"}[d["status"]]
    console.print(
        f"[{color}]{d['status']}[/{color}]: {d['done_files']} copied, {d['skipped_files']} skipped, "
        f"{d['failed_files']} failed ({_size(d['bytes_done'])})"
    )
    if d["error"] and not d["failures"]:
        console.print(f"  [red]x[/red] {d['error']}")
    if d.get("notice"):
        console.print(f"  [yellow]![/yellow] {d['notice']}")
    for failure in d["failures"]:
        console.print(f"  [red]x[/red] {failure['rel']}: {failure['error']}")
    if d["status"] != "completed":
        raise typer.Exit(1)


async def _transfer(direction: Direction, sources: list[str], dest: str, device, conflict, verify) -> None:
    ctx = await _context()
    try:
        device_id = _pick_device(ctx, device)
        queue = ctx.bus.subscribe()
        job = ctx.engine.submit(device_id, direction, sources, dest, conflict=conflict, verify=verify)
        await _follow(ctx, job, queue)
    finally:
        await ctx.stop()


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1")] = 8765,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Do not open the browser")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Start the local web UI."""
    import uvicorn

    from .api.app import create_app

    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    token = secrets.token_urlsafe(24)
    url = f"http://127.0.0.1:{port}/?token={token}"
    application = create_app(token=token, port=port)
    console.print(f"droidxfer running at [bold]{url}[/bold]\nPress Ctrl+C to stop.")
    if not no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(application, host="127.0.0.1", port=port, log_level="info" if verbose else "warning")


@app.command()
@run_async
async def devices() -> None:
    """List connected devices."""
    ctx = await _context()
    try:
        table = Table("id", "name", "android", "state", "connection", "transports")
        for d in ctx.devices.snapshot()["devices"]:
            transports = ", ".join(
                f"{t['kind']}:{t['serial'].removeprefix('mtp:')} ({t['state'].replace('_', ' ')})" for t in d["transports"]
            )
            table.add_row(d["id"], d["name"], d["android_version"], d["state"], d["connection"] or "-", transports)
        console.print(table)
    finally:
        await ctx.stop()


_MARK = {"pass": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]", "unknown": "[dim]?[/dim]"}


@app.command()
@run_async
async def info(device: DeviceOpt = None) -> None:
    """Show detailed phone information and firmware authenticity checks."""
    from .core import deviceinfo

    ctx = await _context()
    try:
        report = await deviceinfo.collect(ctx.remote, _pick_device(ctx, device))
    finally:
        await ctx.stop()
    for section in report["sections"]:
        table = Table(show_header=False, title=section["title"], title_justify="left", box=None, padding=(0, 2))
        for row in section["rows"]:
            table.add_row(f"[dim]{row['label']}[/dim]", row["value"])
        console.print(table)
        console.print()
    if report["checks"]:
        table = Table(show_header=False, title="Authenticity", title_justify="left", box=None, padding=(0, 2))
        for check in report["checks"]:
            table.add_row(_MARK[check["status"]], check["label"], check["value"], f"[dim]{check['detail']}[/dim]")
        console.print(table)
    color = {"pass": "green", "warn": "yellow", "fail": "red"}.get(report["verdict"], "cyan")
    console.print(f"\n[bold {color}]{report['message']}[/bold {color}]")
    for note in report["notes"]:
        console.print(f"[dim]• {note}[/dim]")


@app.command()
@run_async
async def ls(
    path: Annotated[str, typer.Argument(help="Android directory")] = "/sdcard",
    device: DeviceOpt = None,
    all: Annotated[bool, typer.Option("--all", "-a", help="Show hidden files")] = False,
) -> None:
    """List an Android directory."""
    ctx = await _context()
    try:
        listing = await ctx.remote.list(_pick_device(ctx, device), path, show_hidden=all)
        table = Table("name", "size", "modified", title=listing["path"])
        for e in listing["entries"]:
            name = f"[bold blue]{e['name']}/[/bold blue]" if e["is_dir"] else e["name"]
            when = datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M")
            table.add_row(name, "" if e["is_dir"] else _size(e["size"]), when)
        console.print(table)
    finally:
        await ctx.stop()


@app.command()
@run_async
async def push(
    sources: Annotated[list[Path], typer.Argument(help="Mac files/folders followed by the Android folder")],
    device: DeviceOpt = None,
    conflict: ConflictOpt = None,
    verify: VerifyOpt = None,
) -> None:
    """Copy Mac files/folders to Android: push SRC... DEST_DIR."""
    if len(sources) < 2:
        raise ValueError("usage: droidxfer push SRC... DEST_DIR")
    srcs = [str(p.expanduser().resolve()) for p in sources[:-1]]
    await _transfer(Direction.PUSH, srcs, sources[-1].as_posix(), device, conflict, verify)


@app.command()
@run_async
async def pull(
    sources: Annotated[list[str], typer.Argument(help="Android files/folders followed by the Mac folder")],
    device: DeviceOpt = None,
    conflict: ConflictOpt = None,
    verify: VerifyOpt = None,
) -> None:
    """Copy Android files/folders to the Mac: pull SRC... DEST_DIR."""
    if len(sources) < 2:
        raise ValueError("usage: droidxfer pull SRC... DEST_DIR")
    dest = str(Path(sources[-1]).expanduser().resolve())
    await _transfer(Direction.PULL, sources[:-1], dest, device, conflict, verify)


def _split_target(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError("expected HOST:PORT")
    return host.strip("[]"), int(port)


@app.command()
@run_async
async def pair(
    target: Annotated[str, typer.Argument(help="HOST:PORT shown under 'Pair device with pairing code'")],
    code: Annotated[str, typer.Argument(help="6-digit pairing code")],
) -> None:
    """Pair with a phone using Wireless debugging's pairing code."""
    ctx = await _context()
    try:
        console.print(await ctx.wireless.pair(*_split_target(target), code))
    finally:
        await ctx.stop()


@app.command()
@run_async
async def qr() -> None:
    """Show a QR code in the terminal for Wireless debugging > Pair device with QR code."""
    import segno

    ctx = await _context()
    try:
        queue = ctx.bus.subscribe()
        session = ctx.wireless.start_qr()
        segno.make(session["payload"], error="m").terminal(compact=True)
        console.print("On the phone: Developer options > Wireless debugging > Pair device with QR code")
        while True:
            event = await queue.get()
            if event["type"] == "qr_status" and event["data"]["name"] == session["name"]:
                console.print(f"[{event['data']['state']}] {event['data']['message']}")
                if event["data"]["state"] in ("connected", "failed", "expired") or (
                    event["data"]["state"] == "paired" and event["data"]["message"].startswith("Paired.")
                ):
                    break
    finally:
        await ctx.stop()


@app.command()
@run_async
async def connect(target: Annotated[str, typer.Argument(help="HOST:PORT from Wireless debugging")]) -> None:
    """Connect to a phone over Wi-Fi."""
    ctx = await _context()
    try:
        console.print(await ctx.wireless.connect(*_split_target(target)))
    finally:
        await ctx.stop()


@app.command()
@run_async
async def disconnect(target: Annotated[str, typer.Argument(help="adb serial, e.g. HOST:PORT")]) -> None:
    """Disconnect a Wi-Fi device."""
    ctx = await _context()
    try:
        console.print(await ctx.wireless.disconnect(target))
    finally:
        await ctx.stop()


@app.command()
@run_async
async def tcpip(device: DeviceOpt = None, port: Annotated[int, typer.Option()] = 5555) -> None:
    """Switch a USB-connected phone to Wi-Fi (adb tcpip) and connect to it."""
    ctx = await _context()
    try:
        console.print(await ctx.wireless.enable_tcpip(_pick_device(ctx, device), port))
    finally:
        await ctx.stop()


@app.command()
@run_async
async def mdns() -> None:
    """List phones advertising Wireless debugging on the local network."""
    ctx = await _context()
    try:
        table = Table("name", "type", "address")
        for s in await ctx.wireless.discover():
            table.add_row(s["name"], s["type"], f"{s['host']}:{s['port']}")
        console.print(table)
    finally:
        await ctx.stop()


# apps: remove preinstalled apps ----------------------------------------------------
apps_app = typer.Typer(help="Disable, uninstall (for your user) or restore preinstalled apps. Needs USB debugging.",
                       no_args_is_help=True)
app.add_typer(apps_app, name="apps")
_RATING_LABEL = {"safe": "[green]safe[/green]", "probably_safe": "[cyan]probably safe[/cyan]",
                 "advanced": "[yellow]advanced[/yellow]", "unsafe": "[red]don't remove[/red]", "unknown": "[dim]unknown[/dim]"}
ExpertOpt = Annotated[bool, typer.Option("--expert", help="Allow apps rated \"don't remove\" (core parts stay protected).")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", "-n", help="Show what would change without changing anything.")]


def _print_steps(steps: list[dict]) -> int:
    todo = 0
    for s in steps:
        if s["skip"]:
            console.print(f"  [dim]skip[/dim] {s['package']}: {s['skip']}")
        else:
            todo += 1
            console.print(f"  [bold]{s['action']}[/bold] {s['package']}  [dim]{' && '.join(s['commands'])}[/dim]")
            if s.get("warning"):
                console.print(f"    [yellow]! {s['warning']}[/yellow]")
    return todo


def _print_results(results: list[dict]) -> None:
    failed = 0
    for r in results:
        if r["ok"] is None:
            console.print(f"  [dim]skip[/dim] {r['package']}: {r['message']}")
        elif r["ok"]:
            console.print(f"  [green]ok[/green]   {r['package']} ({r['action']})")
        else:
            failed += 1
            console.print(f"  [red]fail[/red] {r['package']}: {r['message']}")
    if failed:
        raise typer.Exit(1)


async def _apps_action(action: str, packages: list[str], device, expert: bool, dry_run: bool) -> None:
    ctx = await _context()
    try:
        device_id = _pick_device(ctx, device)
        if dry_run:
            _print_steps(await ctx.apps.plan(device_id, action, packages, expert=expert))
            console.print("[dim]Dry run: nothing was changed.[/dim]")
        else:
            _print_results(await ctx.apps.apply(device_id, action, packages, expert=expert))
    finally:
        await ctx.stop()


@apps_app.command("list")
@run_async
async def apps_list(
    device: DeviceOpt = None,
    all_apps: Annotated[bool, typer.Option("--all", help="Include apps you installed yourself.")] = False,
    rating: Annotated[str | None, typer.Option(help="safe | probably_safe | advanced | unsafe | unknown")] = None,
    state: Annotated[str | None, typer.Option(help="enabled | disabled | uninstalled")] = None,
    search: Annotated[str | None, typer.Option("--search", "-s", help="Filter by package or name.")] = None,
) -> None:
    """List apps with their safety rating and state."""
    ctx = await _context()
    try:
        listing = await ctx.apps.list(_pick_device(ctx, device))
    finally:
        await ctx.stop()
    rows = [a for a in listing["apps"] if (all_apps or a["system"] or a["preloaded_by"])
            and (not rating or a["rating"] == rating) and (not state or a["state"] == state)
            and (not search or search.lower() in a["package"].lower() or search.lower() in (a["name"] or "").lower())]
    table = Table("package", "name", "rating", "state", "note")
    for a in rows:
        if a["in_use"]:
            note = f"your {a['in_use']}"
        elif a["core"]:
            note = "protected"
        elif a["preloaded_by"]:
            note = f"preloaded by {a['preloaded_by']}"
        else:
            note = "" if a["system"] else "installed by you"
        table.add_row(a["package"], a["name"] or "", _RATING_LABEL.get(a["rating"], a["rating"]), a["state"], note)
    console.print(table)
    status = listing["catalog"]
    community = f"{status['community_count']} community" if status["community_fetched_at"] else "community list not downloaded (droidxfer apps fetch-ratings)"
    console.print(f"[dim]{len(rows)} apps. Ratings: {status['curated_count']} curated, {community}.[/dim]")


@apps_app.command("disable")
@run_async
async def apps_disable(packages: Annotated[list[str], typer.Argument(help="Package names")],
                       device: DeviceOpt = None, expert: ExpertOpt = False, dry_run: DryRunOpt = False) -> None:
    """Disable apps (fully reversible with `apps restore`)."""
    await _apps_action("disable", packages, device, expert, dry_run)


@apps_app.command("uninstall")
@run_async
async def apps_uninstall(packages: Annotated[list[str], typer.Argument(help="Package names")],
                         device: DeviceOpt = None, expert: ExpertOpt = False, dry_run: DryRunOpt = False) -> None:
    """Uninstall apps for your user (preinstalled apps can be restored; their data can't)."""
    await _apps_action("uninstall", packages, device, expert, dry_run)


@apps_app.command("restore")
@run_async
async def apps_restore(packages: Annotated[list[str] | None, typer.Argument(help="Package names")] = None,
                       device: DeviceOpt = None,
                       all_changes: Annotated[bool, typer.Option("--all", help="Undo every change droidxfer made.")] = False,
                       dry_run: DryRunOpt = False) -> None:
    """Re-enable or reinstall apps."""
    if not packages and not all_changes:
        raise ValueError("give package names or --all")
    if all_changes:
        ctx = await _context()
        try:
            device_id = _pick_device(ctx, device)
            if dry_run:
                listing = await ctx.apps.list(device_id)
                changed = [a["package"] for a in listing["apps"] if a["changed_by_droidxfer"]]
                _print_steps(await ctx.apps.plan(device_id, "restore", changed) if changed else [])
                console.print("[dim]Dry run: nothing was changed.[/dim]")
            else:
                _print_results(await ctx.apps.restore_all(device_id))
        finally:
            await ctx.stop()
        return
    await _apps_action("restore", packages, device, False, dry_run)


@apps_app.command("export")
@run_async
async def apps_export(path: Annotated[Path, typer.Argument(help="JSON file to write")], device: DeviceOpt = None) -> None:
    """Save the changes droidxfer made on this phone, to apply them to another phone."""
    import json

    ctx = await _context()
    try:
        doc = await ctx.apps.export(_pick_device(ctx, device))
    finally:
        await ctx.stop()
    path.write_text(json.dumps(doc, indent=2))
    console.print(f"Saved {len(doc['packages'])} changes to {path}")


@apps_app.command("import")
@run_async
async def apps_import(path: Annotated[Path, typer.Argument(help="JSON file from `apps export`")],
                      device: DeviceOpt = None, expert: ExpertOpt = False, dry_run: DryRunOpt = False) -> None:
    """Apply a saved list of changes (shows the plan first with --dry-run)."""
    import json

    ctx = await _context()
    try:
        device_id = _pick_device(ctx, device)
        steps = await ctx.apps.import_plan(device_id, json.loads(path.read_text()), expert=expert)
        if dry_run:
            _print_steps(steps)
            console.print("[dim]Dry run: nothing was changed.[/dim]")
            return
        for action in ("disable", "uninstall"):
            todo = [s["package"] for s in steps if s["action"] == action and not s["skip"]]
            if todo:
                _print_results(await ctx.apps.apply(device_id, action, todo, expert=expert))
    finally:
        await ctx.stop()


@apps_app.command("fetch-ratings")
@run_async
async def apps_fetch_ratings() -> None:
    """Download the community (UAD-ng) app ratings list from GitHub."""
    from .core.appcatalog import UAD_URL, AppCatalog

    console.print(f"Downloading {UAD_URL}")
    status = await asyncio.to_thread(AppCatalog().fetch_community)
    console.print(f"Saved {status['community_count']} community ratings.")


if __name__ == "__main__":
    app()
