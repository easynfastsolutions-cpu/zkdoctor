"""Compact, annotation-sized summary of a `zkdoctor watch` JSONL (standard library only).

Prints GitHub `::notice` workflow commands so the results are readable from the public API
without an artifact download. Records are projected to the fields that matter (heights, findings,
state check, health, and the evidence request/response hash, latency and timestamp).

usage: adi_repro_summary.py watch.jsonl
"""

import json
import sys
from collections import Counter
from datetime import datetime

LIMIT = 3800


def emit(title: str, text: str) -> None:
    text = text if len(text) <= LIMIT else text[: LIMIT - 20] + " ...[truncated]"
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::notice title={title}::{text}")


def evidence(ev: dict | None) -> dict | None:
    if not ev:
        return None
    keys = ("timestamp", "probe_id", "method", "params", "request_hash", "response_hash", "http_status", "latency_ms", "error")
    return {k: ev[k] for k in keys if ev.get(k) is not None}


def compact(rec: dict) -> dict:
    if rec["kind"] != "poll":
        return rec
    def node(n):
        base = {k: n.get(k) for k in ("height", "changed", "seconds_since_change", "error")}
        return {**base, "evidence": evidence(n.get("evidence"))}
    state = rec["state_check"]
    health = rec["health"]
    return {
        **{k: rec[k] for k in ("seq", "timestamp", "status", "findings", "blocks_behind", "fail_streak")},
        "target": node(rec["target"]),
        "reference": node(rec["reference"]),
        "state_check": {k: state.get(k) for k in ("status", "common_height", "target_hash", "reference_hash", "reason")},
        "health": {"status": health["status"], "detail": health.get("detail"), "evidence": evidence(health.get("evidence"))},
    }


def dump(rec: dict) -> str:
    return json.dumps(compact(rec), separators=(",", ":"))


def main(path: str) -> None:
    records = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    metas = [r for r in records if r["kind"] == "meta"]
    polls = [r for r in records if r["kind"] == "poll"]
    ends = [r for r in records if r["kind"] == "end"]
    if not polls:
        emit("watch-summary", f"no poll records ({len(records)} records; meta={bool(metas)} end={ends[-1] if ends else None})")
        return

    codes = Counter(f["code"] for p in polls for f in p["findings"])
    lines = [
        f"polls={len(polls)} span={polls[0]['timestamp'][11:19]}..{polls[-1]['timestamp'][11:19]} UTC",
        f"poll status: {dict(Counter(p['status'] for p in polls))}",
        f"finding codes: {dict(codes)}",
        f"state_check: {dict(Counter(p['state_check']['status'] for p in polls))}",
        f"health: {dict(Counter(p['health']['status'] for p in polls))}",
        f"end record: {ends[-1] if ends else 'none (run cut off)'}",
    ]
    heights = [(p["seq"], p["target"]["height"]) for p in polls if p["target"]["height"] is not None]
    if len(heights) >= 2:
        (s0, h0), (s1, h1) = heights[0], heights[-1]
        t0 = datetime.fromisoformat(polls[s0 - 1]["timestamp"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(polls[s1 - 1]["timestamp"].replace("Z", "+00:00"))
        secs = max((t1 - t0).total_seconds(), 1)
        lines.append(f"target height {h0} -> {h1} in {secs:.0f}s = {(h1 - h0) / secs:.2f} blocks/s")
        last_change = max((p["seq"] for p in polls if p["target"]["changed"]), default=None)
        lines.append(f"last poll where the target height changed: seq {last_change} of {len(polls)}")
    emit("watch-summary", "\n".join(lines))

    step = max(len(polls) // 40, 1)
    rows = [
        f"{p['seq']:>4} {p['timestamp'][11:19]} {p['status']:<5} tgt={p['target']['height']} ref={p['reference']['height']} "
        f"lag={p['blocks_behind']} stale={'-' if p['target']['seconds_since_change'] is None else int(p['target']['seconds_since_change'])}s "
        f"{','.join(f['code'] for f in p['findings'])}"
        for p in polls[::step] + ([polls[-1]] if (len(polls) - 1) % step else [])
    ]
    emit("watch-timeline", "\n".join(rows))

    if metas:
        emit("watch-meta", json.dumps(metas[0], separators=(",", ":"))[:LIMIT])
    emit("watch-first-poll", dump(polls[0]))
    for code in ("TARGET_STALLED", "HEALTH_CHECK_FALSE_POSITIVE", "STATE_DIVERGENCE"):
        hit = next((p for p in polls if any(f["code"] == code for f in p["findings"])), None)
        if hit:
            emit(f"watch-first-{code}", dump(hit))
    emit("watch-last-poll", dump(polls[-1]))


if __name__ == "__main__":
    main(sys.argv[1])
