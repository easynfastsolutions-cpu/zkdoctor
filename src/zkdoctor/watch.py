"""Experimental `watch`: does a target endpoint keep up with a reference endpoint?

Validation experiment for the failure mode in ADI-Foundation-Labs/ADI-Stack-EN-Setup-script#21:
a node stays reachable (and may report healthy) while its block height stops advancing and the
reference keeps moving. Not a monitoring product: one target, one reference, read-only, JSONL out.

Reuses the V0 client (`RpcClient.call`), evidence model (`build_evidence`), redaction and status
enum. The classification (`Tracker`) is a pure function of recorded observations, so a JSONL file
can be re-evaluated later without contacting any node (`replay`).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from . import __version__
from .client import RpcClient, RpcError, redact_url, scrub
from .discovery import is_method_missing
from .evidence import build_evidence, sha256_hex
from .models import Evidence, RpcObservation, Status, utc_now

WATCH_SCHEMA = "zkdoctor-watch/0.1"
HEIGHT_METHOD = "eth_blockNumber"
HASH_METHOD = "eth_getBlockByNumber"
_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")


class WatchConfigError(Exception):
    """Invalid target/reference/thresholds: a tool error (exit 2), not a finding."""


def parse_duration(text: str) -> float:
    """'30s', '5m', '2h', '500ms' or a bare number of seconds -> seconds."""
    m = _DURATION.match(str(text))
    if not m:
        raise ValueError(f"invalid duration {text!r} (use e.g. 30s, 5m, 2h)")
    return float(m.group(1)) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, None: 1}[m.group(2)]


@dataclass(frozen=True)
class Thresholds:
    lag_warn_blocks: int = 2
    stale_warn_s: float = 60.0
    stale_fail_s: float = 300.0
    fail_after: int = 2  # consecutive FAIL polls before a non-zero exit

    def validate(self) -> None:
        if self.lag_warn_blocks < 0:
            raise WatchConfigError("--lag-warn-blocks must be >= 0")
        if self.stale_warn_s <= 0 or self.stale_fail_s < self.stale_warn_s:
            raise WatchConfigError("--stale-warn must be > 0 and <= --stale-fail")
        if self.fail_after < 1:
            raise WatchConfigError("--fail-after must be >= 1")

    def as_dict(self) -> dict[str, Any]:
        return {
            "lag_warn_blocks": self.lag_warn_blocks,
            "stale_warn_s": self.stale_warn_s,
            "stale_fail_s": self.stale_fail_s,
            "fail_after": self.fail_after,
        }


# ------------------------------------------------------------------ observations


@dataclass
class HeightObs:
    height: int | None
    evidence: Evidence
    error: str | None = None


def _hex_int(value: Any) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return int(value, 16)
    return None


def observe_height(client: RpcClient, probe_id: str) -> HeightObs:
    """One `eth_blockNumber` call. Any failure is an error observation, never a stall."""
    try:
        obs = client.call(HEIGHT_METHOD, [])
    except RpcError as exc:
        return HeightObs(None, build_evidence(probe_id, HEIGHT_METHOD, [], None, [], str(exc)), str(exc))
    evidence = build_evidence(probe_id, HEIGHT_METHOD, [], obs, [])
    if obs.rpc_error:
        return HeightObs(None, evidence, f"RPC error {obs.rpc_error.get('code')}: {obs.rpc_error.get('message')}")
    height = _hex_int(obs.result)
    if height is None:
        return HeightObs(None, evidence, f"unparseable height {str(obs.result)[:40]!r}")
    return HeightObs(height, evidence)


@dataclass
class HashObs:
    status: str  # ok | unsupported | unavailable | error
    block_hash: str | None
    evidence: Evidence
    reason: str | None = None


def observe_hash(client: RpcClient, probe_id: str, height: int, depends_on: list[str]) -> HashObs:
    """Block hash at a given height via the read-only `eth_getBlockByNumber` (no full transactions)."""
    params: list[Any] = [hex(height), False]
    try:
        obs = client.call(HASH_METHOD, params)
    except RpcError as exc:
        return HashObs("error", None, build_evidence(probe_id, HASH_METHOD, params, None, depends_on, str(exc)), str(exc))
    evidence = build_evidence(probe_id, HASH_METHOD, params, obs, depends_on)
    if obs.rpc_error:
        if is_method_missing(obs.rpc_error):
            return HashObs("unsupported", None, evidence, f"{HASH_METHOD} not supported")
        return HashObs("error", None, evidence, f"RPC error {obs.rpc_error.get('code')}: {obs.rpc_error.get('message')}")
    if obs.result is None:
        return HashObs("unavailable", None, evidence, f"block {height} not available on this node")
    block_hash = obs.result.get("hash") if isinstance(obs.result, dict) else None
    if not (isinstance(block_hash, str) and _HASH.match(block_hash)):
        return HashObs("error", None, evidence, "response has no 32-byte block hash")
    return HashObs("ok", block_hash.lower(), evidence)


def check_state(target: RpcClient, reference: RpcClient, target_h: int, reference_h: int) -> dict[str, Any]:
    """Compare block hashes at the SAME height: common_height = min(target, reference)."""
    common = min(target_h, reference_h)
    deps = ["WATCH-TARGET-HEIGHT", "WATCH-REFERENCE-HEIGHT"]
    t = observe_hash(target, "WATCH-TARGET-HASH", common, deps)
    r = observe_hash(reference, "WATCH-REFERENCE-HASH", common, deps)
    record: dict[str, Any] = {
        "common_height": common,
        "target_hash": t.block_hash,
        "reference_hash": r.block_hash,
        "evidence": {"target": t.evidence.model_dump(mode="json"), "reference": r.evidence.model_dump(mode="json")},
    }
    if t.status == "ok" and r.status == "ok":
        same = t.block_hash == r.block_hash
        record.update(status=(Status.PASS if same else Status.FAIL).value, reason=None if same else "block hashes differ")
    elif "error" in (t.status, r.status):
        record.update(status=Status.ERROR.value, reason=t.reason if t.status == "error" else r.reason)
    else:  # unsupported / unavailable on at least one side: cannot compare, not a finding
        record.update(status=Status.SKIP.value, reason=t.reason or r.reason)
    return record


# ----------------------------------------------------------------------- health


@dataclass
class HealthResult:
    status: str  # healthy | unhealthy | unknown | error
    evidence: Evidence
    detail: str | None = None


def make_health_probe(url: str, timeout: float = 10.0, transport: httpx.BaseTransport | None = None) -> Callable[[], HealthResult]:
    """GET an EXPLICITLY provided status URL (never guessed). 2xx JSON with `healthy: true` or
    `status: ok|healthy|up|ready` is healthy; `healthy: false` / `status: unhealthy|down|error` is
    unhealthy; anything else is unknown (not assumed healthy)."""
    if not re.match(r"^https?://[^/\s]+", url):
        raise WatchConfigError("--health-url must be http(s)://host[:port]/path")
    shown = redact_url(url)

    def probe() -> HealthResult:
        stamp = utc_now()
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout, transport=transport) as http:
                response = http.get(url)
        except httpx.HTTPError as exc:
            message = scrub(f"{type(exc).__name__}: {exc}", url)
            return HealthResult("error", build_evidence("WATCH-HEALTH", f"GET {shown}", [], None, [], message), message)
        latency = round((time.perf_counter() - started) * 1000, 2)
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text[:200]
        ok = 200 <= response.status_code < 300
        obs = RpcObservation(
            method=f"GET {shown}", params=[], timestamp=stamp, http_status=response.status_code,
            rpc_error=None if ok else {"code": response.status_code, "message": f"HTTP {response.status_code}"},
            result=body if ok else None, latency_ms=latency,
        )
        evidence = build_evidence("WATCH-HEALTH", f"GET {shown}", [], obs, [])
        if not ok:
            return HealthResult("error", evidence, f"HTTP {response.status_code}")
        return HealthResult(_health_status(body), evidence)

    return probe


def _health_status(body: Any) -> str:
    if isinstance(body, dict):
        if isinstance(body.get("healthy"), bool):
            return "healthy" if body["healthy"] else "unhealthy"
        status = str(body.get("status", "")).lower()
        if status in ("ok", "healthy", "up", "ready"):
            return "healthy"
        if status in ("unhealthy", "down", "error", "fail", "failed"):
            return "unhealthy"
    return "unknown"


# ---------------------------------------------------------------- classification

_RANK = {"PASS": 0, "ERROR": 1, "WARN": 2, "FAIL": 3}


@dataclass
class _Anchor:
    height: int  # this node's height when it last changed (or was first seen / re-seen after an error)
    since: float  # time of that observation
    other: int | None  # the other node's height at that moment


@dataclass
class Assessment:
    status: str
    findings: list[dict[str, str]]
    blocks_behind: int | None
    target_changed: bool | None
    reference_changed: bool | None
    target_stale_s: float | None
    reference_stale_s: float | None
    fail_streak: int


class Tracker:
    """Pure state machine: feed it (time, heights, health, state check) per poll.

    Conservative by construction: an RPC error is its own finding (RPC_ERROR) and resets that
    node's stale timer, so a stall is only asserted from consecutive successful observations.
    """

    def __init__(self, thresholds: Thresholds):
        self.th = thresholds
        self._anchor: dict[str, _Anchor | None] = {"target": None, "reference": None}
        self._gap = {"target": False, "reference": False}
        self._last: dict[str, int | None] = {"target": None, "reference": None}
        self.fail_streak = 0

    def _track(self, side: str, other: str, height: int | None, other_height: int | None, now: float) -> bool | None:
        if height is None:
            self._gap[side] = True
            return None
        anchor = self._anchor[side]
        changed = None if anchor is None else height != anchor.height
        if anchor is None or changed or self._gap[side]:
            known_other = other_height if other_height is not None else self._last[other]
            self._anchor[side] = _Anchor(height, now, known_other)
        elif anchor.other is None and other_height is not None:
            anchor.other = other_height
        self._gap[side] = False
        self._last[side] = height
        return changed

    def update(
        self,
        now: float,
        target: int | None,
        reference: int | None,
        *,
        errors: dict[str, str | None] | None = None,
        health: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> Assessment:
        th = self.th
        t_changed = self._track("target", "reference", target, reference, now)
        r_changed = self._track("reference", "target", reference, target, now)
        findings: list[dict[str, str]] = []

        def add(code: str, status: str, message: str) -> None:
            findings.append({"code": code, "status": status, "message": message})

        if target is None or reference is None:
            for side, height in (("target", target), ("reference", reference)):
                if height is None:
                    detail = (errors or {}).get(side) or "no height"
                    add("RPC_ERROR", "ERROR", f"{side} height unavailable: {detail} (not treated as a stall)")
            return Assessment("ERROR", findings, None, t_changed, r_changed, None, None, self.fail_streak)

        ta, ra = self._anchor["target"], self._anchor["reference"]
        assert ta is not None and ra is not None
        t_stale, r_stale = now - ta.since, now - ra.since
        behind = reference - target
        ref_advanced = ta.other is not None and reference > ta.other  # reference moved while target sat still
        tgt_advanced = ra.other is not None and target > ra.other  # target moved while reference sat still

        target_stalled = False
        if t_stale > th.stale_warn_s:
            if ref_advanced:
                target_stalled = True
                fail = t_stale >= th.stale_fail_s
                add(
                    "TARGET_STALLED", "FAIL" if fail else "WARN",
                    f"target height {target} unchanged for {t_stale:.0f}s while reference advanced "
                    f"{reference - (ta.other or 0)} blocks (now {behind} behind)",
                )
                if fail and health == "healthy":
                    add(
                        "HEALTH_CHECK_FALSE_POSITIVE", "FAIL",
                        f"health check reports healthy while target height has not advanced for {t_stale:.0f}s "
                        f"and the reference advanced {reference - (ta.other or 0)} blocks: the health signal "
                        "conflicts with observed chain progression",
                    )
            else:
                add(
                    "BOTH_STALLED", "WARN",
                    f"neither node advanced for {t_stale:.0f}s (target {target}, reference {reference}): "
                    "quiet chain or both stalled; no evidence about the target specifically",
                )
        elif r_stale > th.stale_warn_s and tgt_advanced:
            add(
                "REFERENCE_STALLED", "WARN",
                f"reference height {reference} unchanged for {r_stale:.0f}s while target advanced: "
                "the reference is not a usable anchor right now",
            )
        if behind > th.lag_warn_blocks and not target_stalled:
            add("TARGET_LAGGING", "WARN", f"target is {behind} blocks behind the reference (warn > {th.lag_warn_blocks})")
        if state and state.get("status") == Status.FAIL.value:
            add(
                "STATE_DIVERGENCE", "FAIL",
                f"block hash differs at common height {state['common_height']}: "
                f"target {state['target_hash']} vs reference {state['reference_hash']}",
            )

        status = max((f["status"] for f in findings), key=_RANK.__getitem__, default="PASS")
        if status == "FAIL":
            self.fail_streak += 1
        else:
            self.fail_streak = 0
        return Assessment(status, findings, behind, t_changed, r_changed, t_stale, r_stale, self.fail_streak)


# ------------------------------------------------------------------ orchestration


@dataclass
class WatchResult:
    exit_code: int
    reason: str
    polls: int


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _identity(client: RpcClient) -> dict[str, str]:
    return {"endpoint": client.redacted_url, "fingerprint": sha256_hex(client.redacted_url)[:16]}


def preflight(target: RpcClient, reference: RpcClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """Chain IDs from both nodes. Anything but two readable, equal chain IDs is a configuration error."""
    out = []
    for name, client in (("target", target), ("reference", reference)):
        try:
            obs = client.call("eth_chainId", [])
        except RpcError as exc:
            raise WatchConfigError(f"cannot read chain ID from {name} ({client.redacted_url}): {exc}") from None
        chain_id = _hex_int(obs.result) if not obs.rpc_error else None
        if chain_id is None:
            raise WatchConfigError(f"{name} ({client.redacted_url}) did not return a usable chain ID")
        evidence = build_evidence(f"WATCH-{name.upper()}-CHAINID", "eth_chainId", [], obs, [])
        out.append({**_identity(client), "chain_id": chain_id, "evidence": evidence.model_dump(mode="json")})
    if out[0]["chain_id"] != out[1]["chain_id"]:
        raise WatchConfigError(
            f"chain ID mismatch: target {out[0]['chain_id']} vs reference {out[1]['chain_id']}; "
            "refusing to compare different chains"
        )
    return out[0], out[1]


def format_line(record: dict[str, Any]) -> str:
    """One concise line per poll. PASS is shown as OK."""
    status = "OK" if record["status"] == "PASS" else record["status"]
    codes = " ".join(f["code"] for f in record["findings"])
    stale = record["target"]["seconds_since_change"]
    show = lambda v: "-" if v is None else v  # noqa: E731
    return (
        f"{record['timestamp'][11:19]}  {status:<5} {codes + ' ' if codes else ''}"
        f"target={show(record['target']['height'])} reference={show(record['reference']['height'])} "
        f"lag={show(record['blocks_behind'])} stale={'-' if stale is None else f'{stale:.0f}s'}"
    )


def run_watch(
    target: RpcClient,
    reference: RpcClient,
    thresholds: Thresholds,
    interval_s: float,
    *,
    output: Path | None = None,
    health: Callable[[], HealthResult] | None = None,
    health_url: str | None = None,
    max_polls: int | None = None,
    duration_s: float | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str, str], None] = lambda status, line: None,
) -> WatchResult:
    thresholds.validate()
    if interval_s <= 0:
        raise WatchConfigError("--interval must be > 0")
    target_id, reference_id = preflight(target, reference)

    sink = None
    if output is not None:
        try:
            sink = open(output, "a", encoding="utf-8")  # append-only: earlier records are never rewritten
        except OSError as exc:
            raise WatchConfigError(f"cannot open {output} for append: {exc}") from None

    def write(record: dict[str, Any]) -> None:
        if sink is not None:
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            sink.flush()

    started = clock()
    write({
        "schema": WATCH_SCHEMA, "kind": "meta", "watch_version": "0.1-experimental", "tool_version": __version__,
        "started": _iso(started), "started_t": started, "target": target_id, "reference": reference_id,
        "chain_id": target_id["chain_id"], "thresholds": thresholds.as_dict(), "interval_s": interval_s,
        "health": {"configured": health is not None, "url": redact_url(health_url) if health_url else None},
        "peer_count": {"status": "SKIP", "reason": "no existing V0 probe for peer count; not collected"},
        "state_check": {"method": HASH_METHOD, "at": "min(target_height, reference_height)"},
    })
    emit("INFO", (
        f"watching target={target.redacted_url} reference={reference.redacted_url} chain_id={target_id['chain_id']} "
        f"interval={interval_s:g}s lag>{thresholds.lag_warn_blocks} stale-warn={thresholds.stale_warn_s:g}s "
        f"stale-fail={thresholds.stale_fail_s:g}s fail-after={thresholds.fail_after} "
        f"health={'configured' if health else 'unsupported'} peer_count=unsupported"
    ))

    tracker = Tracker(thresholds)
    polls = usable = 0
    reason = "interrupted"
    try:
        while True:
            polls += 1
            now = clock()
            t, r = observe_height(target, "WATCH-TARGET-HEIGHT"), observe_height(reference, "WATCH-REFERENCE-HEIGHT")
            health_result = health() if health is not None else None
            state = check_state(target, reference, t.height, r.height) if t.height is not None and r.height is not None else None
            a = tracker.update(
                now, t.height, r.height, errors={"target": t.error, "reference": r.error},
                health=health_result.status if health_result else None, state=state,
            )
            usable += t.height is not None and r.height is not None
            record = {
                "schema": WATCH_SCHEMA, "kind": "poll", "seq": polls, "timestamp": _iso(now), "t": now,
                "status": a.status, "findings": a.findings, "blocks_behind": a.blocks_behind, "fail_streak": a.fail_streak,
                "target": {**_identity(target), "height": t.height, "changed": a.target_changed,
                           "seconds_since_change": a.target_stale_s, "error": t.error,
                           "evidence": t.evidence.model_dump(mode="json")},
                "reference": {**_identity(reference), "height": r.height, "changed": a.reference_changed,
                              "seconds_since_change": a.reference_stale_s, "error": r.error,
                              "evidence": r.evidence.model_dump(mode="json")},
                "state_check": state if state else {"status": "SKIP", "reason": "heights unavailable"},
                "health": ({"status": health_result.status, "detail": health_result.detail,
                            "evidence": health_result.evidence.model_dump(mode="json")}
                           if health_result else {"status": "unsupported", "reason": "no --health-url provided"}),
                "peer_count": {"status": "SKIP", "reason": "not collected"},
            }
            write(record)
            emit(a.status, format_line(record))
            if a.fail_streak >= thresholds.fail_after:
                reason = "sustained_fail"
                break
            if max_polls is not None and polls >= max_polls:
                reason = "max_polls"
                break
            if duration_s is not None and clock() - started >= duration_s:
                reason = "duration"
                break
            sleep(interval_s)
    except KeyboardInterrupt:
        reason = "interrupted"
    if reason == "sustained_fail":
        code = 1
    elif polls and not usable and reason != "interrupted":
        reason, code = "no_usable_observations", 2
    else:
        code = 0
    write({"schema": WATCH_SCHEMA, "kind": "end", "ended": _iso(clock()), "reason": reason, "polls": polls, "exit_code": code})
    if sink is not None:
        sink.close()
    return WatchResult(code, reason, polls)


# ------------------------------------------------------------------------ replay


def replay(path: Path) -> list[dict[str, Any]]:
    """Re-evaluate a JSONL file offline (no network): returns [{seq, status, codes}] recomputed from the
    recorded observations and thresholds. Each `meta` record starts a new session."""
    results: list[dict[str, Any]] = []
    tracker: Tracker | None = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("kind") == "meta":
            tracker = Tracker(Thresholds(**rec["thresholds"]))
        elif rec.get("kind") == "poll" and tracker is not None:
            health = rec["health"]["status"]
            a = tracker.update(
                rec["t"], rec["target"]["height"], rec["reference"]["height"],
                errors={"target": rec["target"].get("error"), "reference": rec["reference"].get("error")},
                health=None if health == "unsupported" else health, state=rec["state_check"],
            )
            results.append({"seq": rec["seq"], "status": a.status, "codes": [f["code"] for f in a.findings]})
    return results
