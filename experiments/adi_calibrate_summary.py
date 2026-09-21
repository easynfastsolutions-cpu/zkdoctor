#!/usr/bin/env python3
"""Summarise the calibration run as GitHub `::notice` annotations (readable through the public API).

Inputs (all under out/): samples.jsonl, cleanup.tsv, df-before.txt, df-after.txt.
Projects the disk needed to reach the chain tip two ways and states a verdict. Standard library only.
"""

import json
import os
import statistics
import sys

GIB = 1024**3
DATA = os.environ.get("DATA_DIR", "en/mainnet_data")
N_TIP = int(os.environ.get("N_TIP", "1390000"))  # tip at run time plus a few hours of live blocks
ANCHOR_BLOCKS, ANCHOR_DATA_GIB = 1_364_286, 76.0  # failed run: `du -sh` of the data dir at death (GiB units)
ANCHOR_USED_DELTA_GIB = 86.0  # same run: disk used 59G -> 145G (`df -h`, GiB units)
LIMIT = 3800


def emit(title: str, text: str) -> None:
    text = text if len(text) <= LIMIT else text[: LIMIT - 20] + " ...[truncated]"
    print(f"::notice title={title}::" + text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))


def g(x: float | None) -> str:
    return "n/a" if x is None else f"{x / GIB:.1f}"


def main(out: str = "out") -> None:
    samples = [json.loads(line) for line in open(f"{out}/samples.jsonl", encoding="utf-8") if line.strip()]
    samples = [s for s in samples if s["height"] is not None and s["root"]]
    cleanup = [line.rstrip("\n").split("\t") for line in open(f"{out}/cleanup.tsv", encoding="utf-8") if line.strip()]
    size_b, used_b, avail_b = map(int, open(f"{out}/df-before.txt").read().split())
    size_a, used_a, avail_a = map(int, open(f"{out}/df-after.txt").read().split())

    # 1. disk: before / after cleanup
    rows = sorted(((int(f) if f.lstrip("-").isdigit() else 0, d, s) for d, s, f in cleanup), reverse=True)
    lines = [
        f"root disk {g(size_b)} GiB total | used {g(used_b)} | FREE BEFORE cleanup {g(avail_b)} GiB",
        f"FREE AFTER cleanup {g(avail_a)} GiB  (freed {g(avail_a - avail_b)} GiB)",
        "freed per item (GiB, by df delta | du size):",
    ] + [f"  {f / GIB:6.1f} | {int(s) / GIB:6.1f}  {d}" for f, d, s in rows if f > 50_000_000 or int(s) > 50_000_000]
    emit("cal-1-disk-cleanup", "\n".join(lines))

    if len(samples) < 3:
        emit("cal-error", f"only {len(samples)} usable samples")
        return
    first, last = samples[0], samples[-1]
    dh = max(last["height"] - first["height"], 1)

    # 2. time series
    step = max(len(samples) // 22, 1)
    ts = ["min  height   used  data   blk_dumps  dockerlog  ctr_mem(GiB)"]
    for s in samples[::step] + ([last] if (len(samples) - 1) % step else []):
        tree = s["data_tree"]
        dumps = sum(v for k, v in tree.items() if k.rstrip("/").endswith("block_dumps"))
        ts.append(f"{s['t'] / 60:4.1f} {s['height']:>7} {g(s['root']['used']):>6} {g(tree.get(DATA)):>5} {g(dumps):>9} "
                  f"{g(s['container_log_bytes']):>9} {g(s['container_mem_bytes']):>8}")
    emit("cal-2-timeseries", "\n".join(ts))

    # 3. per-directory breakdown at the last sample
    tree = last["data_tree"]
    total = tree.get(DATA, 0) or 1
    base_tree = first["data_tree"]
    bd = [f"data dir {g(total)} GiB after {dh:,} blocks ({(last['t'] - first['t']) / 60:.0f} min); bytes/block by directory:"]
    for name, size in sorted(tree.items(), key=lambda kv: -kv[1])[:16]:
        per_block = (size - base_tree.get(name, 0)) / dh
        bd.append(f"  {g(size):>6} GiB {100 * size / total:5.1f}%  {per_block / 1024:8.1f} KiB/blk  {name.replace(DATA, '.')}")
    emit("cal-3-directories", "\n".join(bd))

    # 4. memory
    cm = [s["container_mem_bytes"] for s in samples if s["container_mem_bytes"]]
    hm = [s["host_mem_available_bytes"] for s in samples if s["host_mem_available_bytes"]]
    peak = max((s["cgroup_mem_peak_bytes"] or 0 for s in samples), default=0)
    emit("cal-4-memory", "\n".join([
        f"container memory (docker stats): first {g(cm[0])} | median {g(statistics.median(cm))} | max {g(max(cm))} | last {g(cm[-1])} GiB",
        f"cgroup memory.peak: {g(peak) if peak else 'not available'} GiB",
        f"host memory available: min {g(min(hm))} GiB of {g(last['root'] and (last['host_mem_used_bytes'] or 0) + last['host_mem_available_bytes'])} GiB total",
        "note: docker stats includes reclaimable page cache; host 'available' is the safer measure of real pressure",
    ]))

    # 5. projection to the tip
    per_block = lambda a, b: (b - a) / dh  # noqa: E731
    used_pb = per_block(first["root"]["used"], last["root"]["used"])
    data_pb = per_block(first["data_tree"].get(DATA, 0), last["data_tree"].get(DATA, 0))
    log_pb = per_block(first["container_log_bytes"], last["container_log_bytes"])
    root_pb = per_block(first["docker_root_bytes"], last["docker_root_bytes"])
    other_pb = used_pb - data_pb
    n0 = first["height"]
    a_used = first["root"]["used"] + used_pb * (N_TIP - n0)  # A: everything linear from this window
    b_used = first["root"]["used"] + (ANCHOR_DATA_GIB * GIB) * (N_TIP / ANCHOR_BLOCKS) + max(other_pb, 0) * (N_TIP - n0)  # B: data anchored
    disk = last["root"]["size"]
    cross = max(other_pb, 0) * ANCHOR_BLOCKS / GIB
    proj = [
        f"window: {dh:,} blocks. per block: total {used_pb / 1024:.1f} KiB | data dir {data_pb / 1024:.1f} KiB | other {other_pb / 1024:.1f} KiB "
        f"(docker log {log_pb / 1024:.2f} KiB, docker root {root_pb / 1024:.1f} KiB)",
        f"disk after cleanup: {g(disk)} GiB total, {g(first['root']['used'])} GiB used at node start (image included), tip = {N_TIP:,} blocks",
        f"A (all linear from this window):        used at tip {g(a_used)} GiB -> margin {g(disk - a_used)} GiB",
        f"B (data dir anchored to the failed run: {ANCHOR_DATA_GIB:.0f} GiB @ {ANCHOR_BLOCKS:,}): used at tip {g(b_used)} GiB -> margin {g(disk - b_used)} GiB",
        f"cross-check: 'other' (non-data) at the failed run's {ANCHOR_BLOCKS:,} blocks predicted {cross:.1f} GiB vs ~{ANCHOR_USED_DELTA_GIB - ANCHOR_DATA_GIB:.0f} GiB measured then",
    ]
    margin = min(disk - a_used, disk - b_used) / GIB
    verdict = "COMFORTABLE" if margin >= 25 else "TIGHT" if margin >= 10 else "INSUFFICIENT"
    proj.append(f"worst-case margin {margin:.1f} GiB -> {verdict} (comfortable >= 25 GiB, tight 10-25, insufficient < 10)")
    proj.append("caveat: composition and per-block size can change along the chain; this window is the first "
                f"{100 * dh / N_TIP:.0f}% of blocks")
    emit("cal-5-projection", "\n".join(proj))


if __name__ == "__main__":
    main(*sys.argv[1:])
