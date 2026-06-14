"""
flasher-client CLI entry point.

Usage:
    flasher-client [OPTIONS] COMMAND [ARGS]...

Global options (override env vars):
    --host TEXT     Service host  [env: FLASHER_HOST, default: localhost]
    --port INTEGER  Service port  [env: FLASHER_PORT, default: 8080]
    --token TEXT    ******  [env: FLASHER_TOKEN]
    --timeout INT   HTTP timeout  [env: FLASHER_TIMEOUT, default: 30]
    --json          Emit raw JSON output
    -q / --quiet    Suppress non-error output
"""

from __future__ import annotations

import sys
import time
from typing import Optional

import click

from .client import FlasherClient, FlasherClientError
from .config import build_base_url, resolve_host, resolve_port, resolve_timeout, resolve_token

# Terminal phases that end polling
_TERMINAL_PHASES = {"success", "failed", "cancelled"}

# Exit codes
_EXIT_OK = 0
_EXIT_FLASH_FAILED = 1
_EXIT_HTTP_ERROR = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_json(obj: object) -> None:
    import json
    click.echo(json.dumps(obj, indent=2))


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401
        return True
    except ImportError:
        return False


def _make_client(ctx: click.Context) -> FlasherClient:
    cfg = ctx.obj
    base_url = build_base_url(cfg["host"], cfg["port"])
    return FlasherClient(base_url=base_url, token=cfg["token"], timeout=cfg["timeout"])


def _handle_error(exc: FlasherClientError, quiet: bool) -> None:
    if not quiet:
        click.echo(f"Error {exc.status_code}: {exc.detail}", err=True)
    sys.exit(_EXIT_HTTP_ERROR)


def _handle_connect_error(exc: Exception, quiet: bool) -> None:
    if not quiet:
        click.echo(f"Connection error: {exc}", err=True)
    sys.exit(_EXIT_HTTP_ERROR)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.option("--host", default=None, help="Service host [env: FLASHER_HOST]")
@click.option("--port", default=None, type=int, help="Service port [env: FLASHER_PORT]")
@click.option("--token", default=None, help="****** [env: FLASHER_TOKEN]")
@click.option("--timeout", default=None, type=int, help="HTTP timeout seconds [env: FLASHER_TIMEOUT]")
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit raw JSON output")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress non-error output")
@click.pass_context
def cli(
    ctx: click.Context,
    host: Optional[str],
    port: Optional[int],
    token: Optional[str],
    timeout: Optional[int],
    output_json: bool,
    quiet: bool,
) -> None:
    """flasher-client — CLI client for flasher-service."""
    ctx.ensure_object(dict)
    ctx.obj["host"] = resolve_host(host)
    ctx.obj["port"] = resolve_port(port)
    ctx.obj["token"] = resolve_token(token)
    ctx.obj["timeout"] = resolve_timeout(timeout)
    ctx.obj["output_json"] = output_json
    ctx.obj["quiet"] = quiet


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Check service liveness (GET /health)."""
    cfg = ctx.obj
    try:
        with _make_client(ctx) as client:
            data = client.health()
    except FlasherClientError as exc:
        _handle_error(exc, cfg["quiet"])
    except Exception as exc:
        _handle_connect_error(exc, cfg["quiet"])

    ok = data.get("status") == "ok"
    if cfg["output_json"]:
        _print_json(data)
    elif not cfg["quiet"]:
        click.echo("✓ Service is healthy" if ok else f"✗ Unexpected response: {data}")
    sys.exit(_EXIT_OK if ok else _EXIT_HTTP_ERROR)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command("status")
@click.option("--watch", is_flag=True, default=False, help="Poll until terminal phase")
@click.option("--interval", default=2, show_default=True, type=float, help="Poll interval seconds")
@click.pass_context
def status_cmd(ctx: click.Context, watch: bool, interval: float) -> None:
    """Show current flash job status (GET /status)."""
    cfg = ctx.obj

    def _show(data: dict) -> None:
        if cfg["output_json"]:
            _print_json(data)
        elif not cfg["quiet"]:
            _print_status_rich(data)

    try:
        with _make_client(ctx) as client:
            if not watch:
                data = client.status()
                _show(data)
                return

            # Watch mode
            while True:
                data = client.status()
                if cfg["output_json"]:
                    _print_json(data)
                elif not cfg["quiet"]:
                    _print_status_rich(data)

                phase = data.get("phase", "")
                if phase in _TERMINAL_PHASES:
                    break
                time.sleep(interval)

    except FlasherClientError as exc:
        _handle_error(exc, cfg["quiet"])
    except KeyboardInterrupt:
        if not cfg["quiet"]:
            click.echo("\nInterrupted.", err=True)
        sys.exit(_EXIT_OK)
    except Exception as exc:
        _handle_connect_error(exc, cfg["quiet"])

    phase = data.get("phase", "")
    if phase == "success":
        sys.exit(_EXIT_OK)
    elif phase in ("failed", "cancelled"):
        if not cfg["quiet"]:
            click.echo(f"Flash ended: {phase}. Error: {data.get('last_error')}", err=True)
        sys.exit(_EXIT_FLASH_FAILED)


def _print_status_rich(data: dict) -> None:
    """Pretty-print status. Falls back to plain text when Rich is unavailable."""
    if _rich_available():
        from rich.table import Table
        from rich import print as rprint

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Key", style="bold cyan")
        table.add_column("Value")

        phase = data.get("phase", "")
        phase_color = {
            "idle": "white",
            "downloading": "yellow",
            "flashing": "yellow",
            "mfg_staging": "yellow",
            "mfg_flashing": "yellow",
            "verifying": "yellow",
            "success": "green",
            "failed": "red",
            "cancelled": "magenta",
        }.get(phase, "white")

        for key, value in data.items():
            if key == "phase":
                table.add_row(key, f"[{phase_color}]{value}[/{phase_color}]")
            elif value is not None:
                table.add_row(key, str(value))

        rprint(table)
    else:
        for key, value in data.items():
            if value is not None:
                click.echo(f"  {key}: {value}")


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def devices(ctx: click.Context) -> None:
    """List block devices and NXP USB devices (GET /devices)."""
    cfg = ctx.obj
    try:
        with _make_client(ctx) as client:
            data = client.devices()
    except FlasherClientError as exc:
        _handle_error(exc, cfg["quiet"])
    except Exception as exc:
        _handle_connect_error(exc, cfg["quiet"])

    if cfg["output_json"]:
        _print_json(data)
        return

    if cfg["quiet"]:
        return

    block_devs = data.get("devices", [])
    nxp_devs = data.get("nxp_usb_devices", [])

    if _rich_available():
        from rich.table import Table
        from rich import print as rprint

        if block_devs:
            t = Table(title="Block Devices", show_lines=True)
            # Determine columns from first device entry
            if isinstance(block_devs[0], dict):
                cols = list(block_devs[0].keys())
                for col in cols:
                    t.add_column(col)
                for dev in block_devs:
                    t.add_row(*[str(dev.get(c, "")) for c in cols])
            else:
                t.add_column("Device")
                for dev in block_devs:
                    t.add_row(str(dev))
            rprint(t)
        else:
            click.echo("No block devices found.")

        if nxp_devs:
            t2 = Table(title="NXP USB Devices", show_lines=True)
            t2.add_column("Device")
            for dev in nxp_devs:
                t2.add_row(str(dev))
            rprint(t2)
    else:
        click.echo("Block devices:")
        for dev in block_devs:
            click.echo(f"  {dev}")
        if nxp_devs:
            click.echo("NXP USB devices:")
            for dev in nxp_devs:
                click.echo(f"  {dev}")


# ---------------------------------------------------------------------------
# flash
# ---------------------------------------------------------------------------

def _auto_compression(url: str) -> str:
    """Guess compression from URL extension."""
    lower = url.lower()
    if lower.endswith(".gz"):
        return "gzip"
    if lower.endswith(".xz"):
        return "xz"
    if lower.endswith(".zst") or lower.endswith(".zstd"):
        return "zstd"
    return "none"


@cli.command("flash")
@click.argument("image_url")
@click.option(
    "--compression",
    type=click.Choice(["none", "gzip", "xz", "zstd", "auto"]),
    default="auto",
    show_default=True,
    help="Compression format ('auto' detects from URL extension)",
)
@click.option("--sha256", "expected_sha256", default=None, help="Expected SHA-256 of uncompressed image (64-char hex)")
@click.option("--expected-size", "expected_uncompressed_size", default=None, type=int, help="Expected uncompressed byte count")
@click.option("--device", "target_device", default=None, help="Target block device (e.g. /dev/mmcblk1)")
@click.option("--reboot-on-success", is_flag=True, default=False, help="Reboot after successful flash")
@click.option(
    "--method",
    "flash_method",
    type=click.Choice(["direct", "uuu"]),
    default="direct",
    show_default=True,
    help="Flash method",
)
@click.option("--uuu-profile", default=None, help="UUU -b profile (uuu method only)")
@click.option("--uuu-args", multiple=True, help="Custom UUU args; repeatable (uuu method only)")
@click.option("--mfg-usb-path", default=None, help="UUU USB path override (uuu method only)")
@click.option("--wait", "wait_for_completion", is_flag=True, default=False, help="Poll status until terminal phase")
@click.option("--interval", default=2, show_default=True, type=float, help="Poll interval seconds (with --wait)")
@click.pass_context
def flash_cmd(
    ctx: click.Context,
    image_url: str,
    compression: str,
    expected_sha256: Optional[str],
    expected_uncompressed_size: Optional[int],
    target_device: Optional[str],
    reboot_on_success: bool,
    flash_method: str,
    uuu_profile: Optional[str],
    uuu_args: tuple[str, ...],
    mfg_usb_path: Optional[str],
    wait_for_completion: bool,
    interval: float,
) -> None:
    """Start a flash operation (POST /flash).

    IMAGE_URL is the http/https URL of the image to flash.
    """
    cfg = ctx.obj

    resolved_compression = _auto_compression(image_url) if compression == "auto" else compression

    try:
        with _make_client(ctx) as client:
            data = client.flash(
                image_url=image_url,
                compression=resolved_compression,
                expected_sha256=expected_sha256,
                expected_uncompressed_size=expected_uncompressed_size,
                target_device=target_device,
                reboot_on_success=reboot_on_success,
                flash_method=flash_method,
                uuu_profile=uuu_profile or None,
                uuu_args=list(uuu_args) if uuu_args else None,
                mfg_usb_path=mfg_usb_path,
            )

            if cfg["output_json"]:
                _print_json(data)
            elif not cfg["quiet"]:
                click.echo(f"✓ {data.get('message', 'Flash started')}")
                click.echo(f"  target: {data.get('target_device')}")
                click.echo(f"  source: {data.get('source_url')}")

            if not wait_for_completion:
                return

            # Poll until done
            if not cfg["quiet"] and not cfg["output_json"]:
                click.echo("Waiting for completion…")

            final: dict = {}
            while True:
                final = client.status()
                phase = final.get("phase", "")

                if cfg["output_json"]:
                    _print_json(final)
                elif not cfg["quiet"]:
                    _print_status_rich(final)

                if phase in _TERMINAL_PHASES:
                    break
                time.sleep(interval)

    except FlasherClientError as exc:
        _handle_error(exc, cfg["quiet"])
    except KeyboardInterrupt:
        if not cfg["quiet"]:
            click.echo("\nInterrupted.", err=True)
        sys.exit(_EXIT_OK)
    except Exception as exc:
        _handle_connect_error(exc, cfg["quiet"])

    if wait_for_completion:
        phase = final.get("phase", "")
        if phase == "success":
            if not cfg["quiet"] and not cfg["output_json"]:
                click.echo("✓ Flash completed successfully.")
            sys.exit(_EXIT_OK)
        else:
            if not cfg["quiet"]:
                click.echo(
                    f"✗ Flash ended: {phase}. Error: {final.get('last_error')}",
                    err=True,
                )
            sys.exit(_EXIT_FLASH_FAILED)


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def cancel(ctx: click.Context) -> None:
    """Cancel the running flash operation (POST /cancel)."""
    cfg = ctx.obj
    try:
        with _make_client(ctx) as client:
            data = client.cancel()
    except FlasherClientError as exc:
        _handle_error(exc, cfg["quiet"])
    except Exception as exc:
        _handle_connect_error(exc, cfg["quiet"])

    if cfg["output_json"]:
        _print_json(data)
    elif not cfg["quiet"]:
        click.echo(f"✓ {data.get('message', 'Cancellation requested')}")


# ---------------------------------------------------------------------------
# reboot
# ---------------------------------------------------------------------------

@cli.command()
@click.option("-y", "--yes", is_flag=True, default=False, help="Skip confirmation prompt")
@click.pass_context
def reboot(ctx: click.Context, yes: bool) -> None:
    """Reboot the target board (POST /reboot)."""
    cfg = ctx.obj

    if not yes and not cfg["quiet"]:
        click.confirm("Reboot the target board?", abort=True)

    try:
        with _make_client(ctx) as client:
            data = client.reboot()
    except FlasherClientError as exc:
        _handle_error(exc, cfg["quiet"])
    except Exception as exc:
        _handle_connect_error(exc, cfg["quiet"])

    if cfg["output_json"]:
        _print_json(data)
    elif not cfg["quiet"]:
        click.echo(f"✓ {data.get('message', 'Rebooting')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
