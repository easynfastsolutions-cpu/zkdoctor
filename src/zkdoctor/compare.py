"""Baseline-vs-target comparison.

Only compatibility-relevant things are compared: capabilities, response *structure*,
probe status, and an explicit list of identity values. Dynamic values (block numbers,
timestamps, prices, hashes of current state) are never compared.
"""

from __future__ import annotations

from typing import Any

from .models import (
    Change,
    Comparison,
    ComparisonSide,
    Difference,
    Impact,
    ProbeResult,
    Scan,
    Status,
)

# Capabilities whose loss is BREAKING. Everything else that disappears is a WARNING
# (ZKsync OS interfaces are documented as unstable). Extend with `--critical`.
DEFAULT_CRITICAL = frozenset({"eth_chainId", "eth_blockNumber"})

# The complete list of identity values compared, with the impact of a change.
IDENTITY_RULES: dict[str, Impact] = {
    "chain_id": Impact.BREAKING,  # invalidates signatures/replay protection for every client
    "net_version": Impact.WARNING,
    "client_version": Impact.WARNING,
    "detected_execution_environment": Impact.WARNING,
    "execution_version": Impact.WARNING,
}
LABELS = {
    "chain_id": "chain ID",
    "net_version": "net version",
    "client_version": "client version",
    "detected_execution_environment": "detected execution environment",
    "execution_version": "execution version",
}


def _diff_shapes(b: Any, t: Any, path: str, out: list[tuple[str, str, Any, Any]]) -> None:
    """Collect (kind, path, baseline, target) for structural differences."""
    if b == t or b == "..." or t == "...":
        return
    if isinstance(b, dict) and isinstance(t, dict):
        for key in b.keys() - t.keys():
            out.append(("removed", f"{path}.{key}" if path else key, b[key], None))
        for key in t.keys() - b.keys():
            out.append(("added", f"{path}.{key}" if path else key, None, t[key]))
        for key in b.keys() & t.keys():
            _diff_shapes(b[key], t[key], f"{path}.{key}" if path else key, out)
        return
    if isinstance(b, list) and isinstance(t, list):
        if b and t:  # an empty list tells us nothing about element type
            _diff_shapes(b[0], t[0], f"{path}[]", out)
        return
    if b == "null" or t == "null":  # nullable fields legitimately flip with chain state
        return
    if {b, t} == {"integer", "number"}:
        return
    out.append(("type", path or "(root)", b if isinstance(b, str) else _kind(b), t if isinstance(t, str) else _kind(t)))


def _kind(shape: Any) -> str:
    return "object" if isinstance(shape, dict) else "array"


def _shape(result: ProbeResult) -> Any:
    if result.evidence and result.evidence.result_summary:
        return result.evidence.result_summary.get("shape")
    return None


def compare_scans(baseline: Scan, target: Scan, critical: frozenset[str] = DEFAULT_CRITICAL) -> Comparison:
    diffs: list[Difference] = []

    # --- environment identity
    for name, impact in IDENTITY_RULES.items():
        b, t = getattr(baseline.environment, name), getattr(target.environment, name)
        if b is None or t is None or b == t:
            continue  # unknown on one side is a probe problem, reported per probe
        diffs.append(Difference(
            subject="environment", path=name, change=Change.CHANGED, impact=impact,
            message=f"{LABELS[name]} changed", baseline=b, target=t,
        ))
    bm, tm = baseline.environment.protocol_metadata, target.environment.protocol_metadata
    for key in sorted(bm.keys() | tm.keys()):
        if bm.get(key) == tm.get(key):
            continue
        change = Change.REMOVED if key not in tm else Change.ADDED if key not in bm else Change.CHANGED
        diffs.append(Difference(
            subject="environment", path=f"protocol_metadata.{key}", change=change, impact=Impact.WARNING,
            message=f"protocol metadata '{key}' {change.value.lower()}", baseline=bm.get(key), target=tm.get(key),
        ))

    # --- capabilities
    capability_changed: set[str] = set()
    for method in sorted(baseline.capabilities.keys() | target.capabilities.keys()):
        bc, tc = baseline.capabilities.get(method), target.capabilities.get(method)
        if bc is None or tc is None:
            present = bc or tc
            diffs.append(Difference(
                subject="capability", path=method, change=Change.ADDED if bc is None else Change.REMOVED,
                impact=Impact.NONE, message="capability only present in one scan (different tool version?)",
                baseline=bc.supported if bc else None, target=tc.supported if tc else None,
            ))
            capability_changed.add(present.probe_id)
            continue
        if bc.supported is None or tc.supported is None or bc.supported == tc.supported:
            continue
        capability_changed.add(bc.probe_id)
        if bc.supported:
            impact = Impact.BREAKING if method in critical else Impact.WARNING
            diffs.append(Difference(
                subject=bc.probe_id, path=method, change=Change.REMOVED, impact=impact,
                message=f"capability changed: supported -> unsupported ({method})", baseline=True, target=False,
            ))
        else:
            diffs.append(Difference(
                subject=tc.probe_id, path=method, change=Change.ADDED, impact=Impact.NONE,
                message=f"capability changed: unsupported -> supported ({method})", baseline=False, target=True,
            ))

    # --- probes
    bres = {r.probe_id: r for r in baseline.results}
    tres = {r.probe_id: r for r in target.results}
    unchanged: list[str] = []
    for pid in sorted(bres.keys() | tres.keys()):
        if pid not in bres or pid not in tres:
            continue  # already reported via the capability entry above
        b, t = bres[pid], tres[pid]
        before = len(diffs)
        if pid not in capability_changed:
            diffs.extend(_probe_differences(pid, b, t))
        if len(diffs) == before and pid not in capability_changed:
            unchanged.append(pid)

    order = {Impact.BREAKING: 0, Impact.WARNING: 1, Impact.NONE: 2}
    diffs.sort(key=lambda d: (order[d.impact], d.subject, d.path or ""))
    summary = {
        "breaking": sum(d.impact == Impact.BREAKING for d in diffs),
        "warning": sum(d.impact == Impact.WARNING for d in diffs),
        "other": sum(d.impact == Impact.NONE for d in diffs),
        "unchanged_probes": len(unchanged),
    }
    side = lambda s: ComparisonSide(  # noqa: E731
        scan_id=s.scan_id, timestamp=s.timestamp, endpoint=s.environment.endpoint, tool_version=s.tool_version
    )
    return Comparison(baseline=side(baseline), target=side(target), differences=diffs,
                      unchanged_probes=unchanged, summary=summary)


def _probe_differences(pid: str, b: ProbeResult, t: ProbeResult) -> list[Difference]:
    found: list[tuple[str, str, Any, Any]] = []
    bs, ts = _shape(b), _shape(t)
    if bs is not None and ts is not None:
        _diff_shapes(bs, ts, "", found)
    if found:
        out = []
        for kind, path, bv, tv in found:
            if kind == "removed":
                out.append(Difference(subject=pid, path=path, change=Change.REMOVED, impact=Impact.BREAKING,
                                      message=f"field removed: {path}", baseline=bv))
            elif kind == "added":
                out.append(Difference(subject=pid, path=path, change=Change.ADDED, impact=Impact.NONE,
                                      message=f"field added: {path}", target=tv))
            else:
                out.append(Difference(subject=pid, path=path, change=Change.CHANGED, impact=Impact.BREAKING,
                                      message=f"field type changed: {path}", baseline=bv, target=tv))
        return out

    if b.status == t.status:
        return []
    if Status.ERROR in (b.status, t.status):
        return [Difference(subject=pid, change=Change.CHANGED, impact=Impact.WARNING,
                           message="not comparable: a probe hit a transport/protocol error",
                           baseline=b.status.value, target=t.status.value)]
    if t.status == Status.FAIL:
        impact = Impact.BREAKING
    elif b.status == Status.FAIL:
        impact = Impact.NONE  # improvement
    else:
        impact = Impact.WARNING
    return [Difference(subject=pid, change=Change.CHANGED, impact=impact,
                       message=f"probe status changed: {b.status.value} -> {t.status.value}",
                       baseline=b.status.value, target=t.status.value)]
