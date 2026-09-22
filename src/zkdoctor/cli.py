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
from .watch import Thresholds, WatchConfigError, make_health_probe, parse_duration, run_watch

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


_WATCH_STYLE = {"PASS": "green", "WARN": "yellow", "FAIL": "bold red", "ERROR": "red", "INFO": "dim"}


@app.command()
def watch(
    rpc: Annotated[str, typer.Option("--rpc", help="Target endpoint to watch.")],
    reference: Annotated[str, typer.Option("--reference", help="Reference endpoint on the same chain.")],
    interval: Annotated[str, typer.Option(help="Time between polls, e.g. 30s, 2m.")] = "30s",
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Append JSONL records here.")] = None,
    lag_warn_blocks: Annotated[int, typer.Option(help="WARN when the target is more than this many blocks behind.")] = 2,
    stale_warn: Annotated[str, typer.Option(help="WARN when the target height is unchanged for longer than this.")] = "60s",
    stale_fail: Annotated[str, typer.Option(help="FAIL when unchanged for at least this long while the reference advances.")] = "300s",
    fail_after: Annotated[int, typer.Option(help="Consecutive FAIL polls before exiting non-zero.")] = 2,
    unreachable_after: Annotated[
        int,
        typer.Option(help="FAIL after this many consecutive polls where the target is unreachable while the reference answers (0 disables)."),
    ] = 10,
    health_url: Annotated[
        str | None, typer.Option("--health-url", help="Target status URL to record (never guessed; omit if none).")
    ] = None,
    timeout: Annotated[float, typer.Option(help="Per-request timeout in seconds.")] = 10.0,
    max_polls: Annotated[int | None, typer.Option(help="Stop after this many polls.")] = None,
    duration: Annotated[str | None, typer.Option(help="Stop after this long, e.g. 10m.")] = None,
) -> None:
    """EXPERIMENTAL: does the target keep up with the reference? (block progression, state agreement)"""
    try:
        thresholds = Thresholds(
            lag_warn_blocks, parse_duration(stale_warn), parse_duration(stale_fail), fail_after, unreachable_after
        )
        interval_s = parse_duration(interval)
        duration_s = parse_duration(duration) if duration else None
        target_client, reference_client = RpcClient(rpc, timeout=timeout), RpcClient(reference, timeout=timeout)
        health = make_health_probe(health_url, timeout) if health_url else None
    except (ValueError, WatchConfigError) as exc:
        raise _fail(str(exc)) from None
    try:
        result = run_watch(
            target_client, reference_client, thresholds, interval_s, output=output, health=health,
            health_url=health_url, max_polls=max_polls, duration_s=duration_s,
            emit=lambda status, line: out.print(line, style=_WATCH_STYLE.get(status), soft_wrap=True, markup=False, highlight=False),
        )
    except WatchConfigError as exc:
        raise _fail(str(exc)) from None
    finally:
        target_client.close()
        reference_client.close()
    out.print(f"watch ended: {result.reason} after {result.polls} polls (exit {result.exit_code})", markup=False)
    raise typer.Exit(result.exit_code)


if __name__ == "__main__":  # pragma: no cover
    app()
