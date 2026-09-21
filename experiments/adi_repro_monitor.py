#!/usr/bin/env python3
"""Light resource monitor for the ADI external-node container (GitHub-hosted runner, standard library only).

Every EVERY seconds records: block height, root-disk usage, container memory (docker stats), cgroup memory
(current / peak / events incl. oom and oom_kill), host memory, container log-dir size, container state
(status / OOMKilled / exit code) and the count of kernel OOM lines in dmesg. The first time the container is
no longer running it takes a full diagnostic snapshot (adi_repro_diag.sh death) immediately, while the kernel
ring buffer and Docker's state are fresh, then stops.
"""

import json
import os
import re
import subprocess
import time
import urllib.request

CONTAINER = os.environ.get("EN_CONTAINER", "adi_mainnet_external_node")
EVERY = int(os.environ.get("MONITOR_EVERY", "120"))
OUT = os.environ.get("MONITOR_OUT", "out/monitor.jsonl")
UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "kib": 1024, "mib": 1024**2, "gib": 1024**3}


def sh(cmd: str, timeout: int = 120) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def to_bytes(text: str) -> int | None:
    m = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]+)\s*", text or "")
    return int(float(m.group(1)) * UNITS[m.group(2).lower()]) if m and m.group(2).lower() in UNITS else None


def height() -> int | None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:3050", data=body, headers={"Content-Type": "application/json"}), timeout=8
        ) as r:
            return int(json.loads(r.read())["result"], 16)
    except Exception:
        return None


def cgroup_dir(cid: str) -> str | None:
    for candidate in (f"/sys/fs/cgroup/system.slice/docker-{cid}.scope", f"/sys/fs/cgroup/docker/{cid}"):
        if os.path.isdir(candidate):
            return candidate
    return None


def read_int(path: str) -> int | None:
    try:
        return int(open(path).read().strip())
    except Exception:
        return None


def read_events(path: str) -> dict | None:
    try:
        return {k: int(v) for k, v in (line.split() for line in open(path).read().splitlines())}
    except Exception:
        return None


def sample(start: float, cid: str) -> dict:
    state = sh(f"docker inspect -f '{{{{.State.Status}}}}|{{{{.State.OOMKilled}}}}|{{{{.State.ExitCode}}}}' {CONTAINER}").split("|")
    stats = sh(f"docker stats --no-stream --format '{{{{.MemUsage}}}}' {CONTAINER}").split("/")
    cg = cgroup_dir(cid) if cid else None
    root = sh("df -B1 --output=size,used,avail / | tail -n1").split()
    mem = sh("free -b | awk '/Mem:/ {print $3, $7}; /Swap:/ {print $3}'").split()
    return {
        "t": round(time.time() - start),
        "utc": time.strftime("%H:%M:%S", time.gmtime()),
        "state": {"status": state[0], "oom_killed": state[1], "exit_code": state[2]} if len(state) == 3 else None,
        "height": height(),
        "root": dict(zip(("size", "used", "avail"), map(int, root))) if len(root) == 3 else None,
        "container_mem_bytes": to_bytes(stats[0]) if stats and stats[0] else None,
        "cgroup_mem_current": read_int(f"{cg}/memory.current") if cg else None,
        "cgroup_mem_peak": read_int(f"{cg}/memory.peak") if cg else None,
        "cgroup_mem_events": read_events(f"{cg}/memory.events") if cg else None,
        "host_mem_used": int(mem[0]) if len(mem) >= 2 else None,
        "host_mem_available": int(mem[1]) if len(mem) >= 2 else None,
        "swap_used": int(mem[2]) if len(mem) >= 3 else None,
        "container_dir_bytes": int(sh(f"sudo du -sb /var/lib/docker/containers/{cid} 2>/dev/null | cut -f1") or 0) if cid else None,
        "dmesg_oom_lines": int(sh("sudo dmesg 2>/dev/null | grep -ciE 'out of memory|oom-kill|killed process'") or 0),
    }


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    start = time.time()
    cid = ""
    captured = False
    with open(OUT, "a", encoding="utf-8") as sink:
        while True:
            tick = time.time()
            cid = cid or sh(f"docker inspect -f '{{{{.Id}}}}' {CONTAINER}")
            record = sample(start, cid)
            sink.write(json.dumps(record, separators=(",", ":")) + "\n")
            sink.flush()
            state = record["state"] or {}
            print(f"t={record['t']:>5}s {state.get('status')} height={record['height']} mem={(record['container_mem_bytes'] or 0) / 2**30:.1f}GiB "
                  f"avail={(record['host_mem_available'] or 0) / 2**30:.1f}GiB used={(record['root'] or {}).get('used', 0) / 2**30:.1f}GiB", flush=True)
            if state.get("status") not in (None, "running", "created", "restarting") and not captured:
                captured = True
                print("container is no longer running: taking the death snapshot", flush=True)
                subprocess.run(["bash", "experiments/adi_repro_diag.sh", "death"], check=False)
                break
            time.sleep(max(0.0, EVERY - (time.time() - tick)))


if __name__ == "__main__":
    main()
