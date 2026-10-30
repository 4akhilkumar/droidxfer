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
ConflictOpt = Annotated[str | None, typer.Option("--conflict", "-c", help="rename | overwrite | skip")]
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
            transports = ", ".join(f"{t['kind']}:{t['serial']} ({t['state']})" for t in d["transports"])
            table.add_row(d["id"], d["name"], d["android_version"], d["state"], d["connection"] or "-", transports)
        console.print(table)
    finally:
        await ctx.stop()


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


if __name__ == "__main__":
    app()
