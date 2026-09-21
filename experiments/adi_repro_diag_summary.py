#!/usr/bin/env python3
"""Turn the monitor samples and the death/final diagnostics into annotation-sized notices with an explicit
exit-code / OOM verdict. Standard library only.  usage: adi_repro_diag_summary.py [out_dir]
"""

import json
import os
import re
import statistics
import sys

GIB = 1024**3
LIMIT = 3800
CAL_MEM_GIB = {10: 12.4, 22: 11.7}  # calibration run 35610930698, container memory at 10 / 22 min, UNCAPPED logs
CAL_LOG_GIB_AT_22MIN = 1.3          # same run: container log size after 22 minutes, uncapped


def emit(title: str, text: str) -> None:
    text = text if len(text) <= LIMIT else text[: LIMIT - 20] + " ...[truncated]"
    print(f"::notice title={title}::" + text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))


def g(x) -> str:
    return "n/a" if x is None else f"{x / GIB:.1f}"


def section(text: str, name: str) -> str:
    m = re.search(rf"== {re.escape(name)}[^\n]*\n(.*?)(?=\n== |\Z)", text, re.S)
    return m.group(1).strip() if m else ""


def main(out: str = "out") -> None:
    samples = []
    if os.path.exists(f"{out}/monitor.jsonl"):
        samples = [json.loads(line) for line in open(f"{out}/monitor.jsonl", encoding="utf-8") if line.strip()]
    death = f"{out}/diag-death.txt"
    diag_path = death if os.path.exists(death) else f"{out}/diag-final.txt"
    diag = open(diag_path, encoding="utf-8", errors="replace").read() if os.path.exists(diag_path) else ""
    which = "DEATH snapshot (taken when the container stopped running)" if diag_path == death else "final snapshot (container never died before the run ended)"

    # 1. exit code / OOM verdict
    state = {}
    raw = section(diag, "container state")
    try:
        state = json.loads(raw.splitlines()[0]) if raw else {}
    except Exception:
        pass
    dmesg_oom = [l for l in section(diag, "dmesg: memory / disk / kill lines").splitlines() if re.search(r"out of memory|oom|killed process", l, re.I)]
    events = section(diag, "docker events")
    running = state.get("Running")
    code, oom_flag = state.get("ExitCode"), state.get("OOMKilled")
    if running:
        verdict = "CONTAINER STILL RUNNING at the end: it did not die."
    elif oom_flag:
        verdict = "OOM-KILLED: Docker reports OOMKilled=true (cgroup memory limit / kernel OOM killer)."
    elif dmesg_oom:
        verdict = f"KERNEL OOM evidence in dmesg ({len(dmesg_oom)} lines) although Docker's OOMKilled flag is {oom_flag}."
    elif code == 137:
        verdict = "exit 137 (SIGKILL) with NO OOM evidence in Docker state or dmesg: killed from outside; see disk/kernel sections."
    elif code == 0:
        verdict = "exited with code 0 (clean exit)."
    else:
        verdict = f"exited with code {code}; no OOM evidence in Docker state or dmesg."
    emit("diag-1-exit-and-oom", "\n".join([
        f"source: {which}",
        f"VERDICT: {verdict}",
        f"State: Status={state.get('Status')} Running={running} ExitCode={code} OOMKilled={oom_flag} Error={state.get('Error')!r}",
        f"StartedAt={state.get('StartedAt')} FinishedAt={state.get('FinishedAt')}",
        f"kernel OOM lines in dmesg: {len(dmesg_oom)}; monitor max dmesg_oom_lines: {max((s.get('dmesg_oom_lines') or 0 for s in samples), default='n/a')}",
        "docker events:", events or "(none recorded)", section(diag, "restart count"),
    ]))

    # 2. kernel evidence
    emit("diag-2-kernel", "dmesg (memory / disk / kill lines):\n" + (section(diag, "dmesg: memory / disk / kill lines") or "(none)")
         + "\n\njournal oom lines:\n" + (section(diag, "journal (kernel) oom lines") or "(none)"))

    if samples:
        # 3. memory over time and vs the uncapped calibration
        mem = [s["container_mem_bytes"] for s in samples if s.get("container_mem_bytes")]
        avail = [s["host_mem_available"] for s in samples if s.get("host_mem_available")]
        peak = max((s.get("cgroup_mem_peak") or 0 for s in samples), default=0)
        events = [s["cgroup_mem_events"] for s in samples if s.get("cgroup_mem_events")]
        at = lambda minute: next((s["container_mem_bytes"] for s in samples if s["t"] >= minute * 60 and s.get("container_mem_bytes")), None)  # noqa: E731
        step = max(len(samples) // 24, 1)
        rows = ["min   height    mem_GiB avail_GiB used_disk_GiB"] + [
            f"{s['t'] / 60:5.0f} {str(s['height']):>8} {g(s.get('container_mem_bytes')):>8} {g(s.get('host_mem_available')):>9} {g((s.get('root') or {}).get('used')):>9}"
            for s in samples[::step]
        ]
        lines = [
            f"{len(samples)} samples over {samples[-1]['t'] / 3600:.2f} h",
            f"container memory: first {g(mem[0]) if mem else 'n/a'} | median {g(statistics.median(mem)) if mem else 'n/a'} | max {g(max(mem)) if mem else 'n/a'} | last {g(mem[-1]) if mem else 'n/a'} GiB",
            f"cgroup memory.peak: {g(peak) if peak else 'n/a'} GiB | host available: min {g(min(avail)) if avail else 'n/a'} GiB",
            f"cgroup memory.events (last): {events[-1] if events else 'n/a'}  <- oom / oom_kill count OOM events",
            f"LOG-CAP EFFECT on memory (calibration was uncapped): at 10 min {g(at(10))} vs {CAL_MEM_GIB[10]} GiB; at 22 min {g(at(22))} vs {CAL_MEM_GIB[22]} GiB",
            "", *rows,
        ]
        emit("diag-3-memory", "\n".join(lines))

        # 4. disk and the log cap
        used = [(s["root"] or {}).get("used") for s in samples if s.get("root")]
        avail_disk = [(s["root"] or {}).get("avail") for s in samples if s.get("root")]
        logs = [s["container_dir_bytes"] for s in samples if s.get("container_dir_bytes")]
        at22 = next((s["container_dir_bytes"] for s in samples if s["t"] >= 22 * 60 and s.get("container_dir_bytes")), None)
        emit("diag-4-disk", "\n".join([
            f"root disk used: first {g(used[0])} | last {g(used[-1])} | max {g(max(used))} GiB; MIN FREE {g(min(avail_disk))} GiB",
            f"container dir (logs) size: max {g(max(logs)) if logs else 'n/a'} GiB (cap is 3 x 100 MB); at 22 min {g(at22)} GiB vs {CAL_LOG_GIB_AT_22MIN} GiB uncapped in the calibration",
            "height reached: " + str(max((s['height'] or 0 for s in samples), default='n/a')),
        ]))

    # 5. the last lines the node logged before the snapshot
    tail = section(diag, "docker logs --tail 150").splitlines()[-18:]
    emit("diag-5-node-log-tail", "\n".join(l[:230] for l in tail) or "(no node log captured)")


if __name__ == "__main__":
    main(*sys.argv[1:])
