"""Command line entry point.

Exit codes: 0 no breaking findings, 1 breaking/failed findings, 2 usage or tool error
(including an unreachable endpoint or unreadable scan file).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from . import __version__
from .client import RpcClient
from .compare import DEFAULT_CRITICAL, compare_scans
from .discovery import DiscoveryAborted
from .evidence import ScanFileError, load_scan, save_scan
from .probes import run_scan
from .report import render_comparison, render_scan

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="ZKsync Compatibility Doctor: scan an RPC endpoint, compare two scans.",
)
out = Console()
err = Console(stderr=True)


def _fail(message: str) -> typer.Exit:
    err.print(f"error: {message}", markup=False)
    return typer.Exit(2)


def _print_version(value: bool) -> None:
    if value:
        out.print(f"zkdoctor {__version__}")
        raise typer.Exit(0)


@app.callback()
def _main(
    version: Annotated[
        bool,
        # eager: must run before click demands a subcommand, or `zkdoctor --version` exits 2
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    pass


@app.command()
def scan(
    rpc: Annotated[str, typer.Option("--rpc", help="JSON-RPC endpoint URL (credentials are redacted in output).")],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write the scan JSON here.")] = None,
    timeout: Annotated[float, typer.Option(help="Per-request timeout in seconds.")] = 10.0,
    require: Annotated[
        list[str] | None,
        typer.Option("--require", help="RPC method that must be supported; missing => FAIL. Repeatable."),
    ] = None,
    json_stdout: Annotated[bool, typer.Option("--json", help="Print the scan JSON to stdout instead of the report.")] = False,
) -> None:
    """Run read-only probes against an endpoint and record evidence."""
    try:
        client = RpcClient(rpc, timeout=timeout)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    try:
        result = run_scan(client, frozenset(require or []))
    except DiscoveryAborted as exc:
        raise _fail(f"cannot reach endpoint (this is not a compatibility result): {exc}") from None
    finally:
        client.close()

    if output:
        try:
            save_scan(result, output)
        except OSError as exc:
            raise _fail(f"cannot write {output}: {exc}") from None
    if json_stdout:
        typer.echo(result.model_dump_json(indent=2, by_alias=True))
    else:
        render_scan(result, out, str(output) if output else None)
        if not output:
            out.print("\n(use --output scan.json to save this evidence)", style="dim", markup=False)

    if result.summary.error:
        raise typer.Exit(2)
    raise typer.Exit(1 if result.summary.fail else 0)


@app.command()
def compare(
    baseline: Annotated[Path, typer.Argument(help="Baseline scan JSON.")],
    target: Annotated[Path, typer.Argument(help="Target scan JSON.")],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write the comparison JSON here.")] = None,
    critical: Annotated[
        list[str] | None,
        typer.Option("--critical", help="Method whose loss is BREAKING (in addition to eth_chainId, eth_blockNumber)."),
    ] = None,
    json_stdout: Annotated[bool, typer.Option("--json", help="Print the comparison JSON to stdout instead of the report.")] = False,
) -> None:
    """Compare two saved scans and report compatibility differences."""
    try:
        base, targ = load_scan(baseline), load_scan(target)
    except ScanFileError as exc:
        raise _fail(str(exc)) from None
    result = compare_scans(base, targ, DEFAULT_CRITICAL | frozenset(critical or []))

    if output:
        try:
            output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise _fail(f"cannot write {output}: {exc}") from None
    if json_stdout:
        typer.echo(result.model_dump_json(indent=2))
    else:
        render_comparison(result, out)
    raise typer.Exit(1 if result.summary["breaking"] else 0)


if __name__ == "__main__":  # pragma: no cover
    app()
