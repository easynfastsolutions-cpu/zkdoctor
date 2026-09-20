"""Terminal reports, rendered from the same models that are saved as JSON."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from .models import Change, Comparison, Impact, Scan, Status

_SYMBOLS = {
    Status.PASS: ("✓", "PASS"),
    Status.WARN: ("⚠", "WARN"),
    Status.FAIL: ("✗", "FAIL"),
    Status.SKIP: ("-", "SKIP"),
    Status.ERROR: ("!", "ERR "),
}
_STYLE = {Status.PASS: "green", Status.WARN: "yellow", Status.FAIL: "red", Status.SKIP: "dim", Status.ERROR: "red"}


def _unicode(console: Console) -> bool:
    return (console.encoding or "").lower().replace("-", "").startswith("utf")


def _mark(console: Console, status: Status) -> str:
    symbol, ascii_ = _SYMBOLS[status]
    return symbol if _unicode(console) else ascii_


def _rule(console: Console, width: int = 40) -> str:
    return ("─" if _unicode(console) else "-") * width


def _show(value: object) -> str:
    return escape(str(value)) if value is not None else "-"


def render_scan(scan: Scan, console: Console, output: str | None = None) -> None:
    env = scan.environment
    p = console.print
    p("ZKsync Compatibility Doctor")
    p(_rule(console))
    p("\nEnvironment")
    p(f"  Endpoint:          {_show(env.endpoint)}")
    p(f"  Chain ID:          {_show(env.chain_id)}")
    p(f"  Client:            {_show(env.client_version)}")
    p(f"  Detected:          {_show(env.detected_execution_environment)}")
    p(f"  Execution version: {_show(env.execution_version)}")
    for basis in env.detection_basis:
        p(f"    basis: {_show(basis)}", style="dim")

    generic = [r for r in scan.results if r.category == "generic"]
    zks = [r for r in scan.results if r.category == "zksync"]
    zks_supported = sum(1 for r in zks if scan.capabilities[_method(scan, r.probe_id)].supported)
    p("\nDiscovery")
    p(f"  Generic RPC       {sum(r.status == Status.PASS for r in generic)}/{len(generic)} PASS")
    p(f"  ZKsync RPC        {zks_supported}/{len(zks)} supported")

    p("\nChecks")
    for r in scan.results:
        line = f"  {_mark(console, r.status)} {r.probe_id} {r.name}"
        if r.status != Status.PASS:
            line += f" - {r.summary}"
        p(escape(line), style=_STYLE[r.status])
        if r.status in (Status.FAIL, Status.WARN, Status.ERROR):
            for detail in r.details:
                p(escape(f"      {detail}"), style="dim")

    s = scan.summary
    p("\nSummary")
    p(f"  PASS: {s.passed}   WARN: {s.warn}   FAIL: {s.fail}   SKIP: {s.skip}   ERROR: {s.error}")
    if output:
        p(f"\nEvidence:\n  {escape(output)}")


def _method(scan: Scan, probe_id: str) -> str:
    return next(m for m, c in scan.capabilities.items() if c.probe_id == probe_id)


def render_comparison(cmp: Comparison, console: Console) -> None:
    p = console.print
    p("COMPATIBILITY DIFFERENCES")
    p(_rule(console, 28))
    p(f"baseline: {_show(cmp.baseline.endpoint)}  ({cmp.baseline.timestamp})", style="dim")
    p(f"target:   {_show(cmp.target.endpoint)}  ({cmp.target.timestamp})", style="dim")

    for impact, title, style in (
        (Impact.BREAKING, "BREAKING", "red"),
        (Impact.WARNING, "WARNING", "yellow"),
        (Impact.NONE, "OTHER CHANGES (not breaking)", "cyan"),
    ):
        group = [d for d in cmp.differences if d.impact == impact]
        if not group:
            continue
        p(f"\n{title}", style=f"bold {style}")
        for d in group:
            p(escape(f"\n{d.subject}"), style="bold")
            p(escape(f"  {d.message}"))
            if d.change == Change.REMOVED and d.target is None:
                p(escape(f"  was: {_show(d.baseline)}"), style="dim")
            elif d.change == Change.ADDED and d.baseline is None:
                p(escape(f"  now: {_show(d.target)}"), style="dim")
            elif d.baseline is not None or d.target is not None:
                p(escape(f"  {_show(d.baseline)} -> {_show(d.target)}"), style="dim")

    if not cmp.differences:
        p("\nNo compatibility differences found.")
    p(f"\nUNCHANGED\n  {len(cmp.unchanged_probes)} probes")
    s = cmp.summary
    p(f"\nSummary\n  BREAKING: {s['breaking']}   WARNING: {s['warning']}   OTHER: {s['other']}")
