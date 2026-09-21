#!/usr/bin/env python3
"""Calibration sampler: while an ADI external node syncs for a short, fixed time, record once a minute
the block height, filesystem usage, a per-directory size tree of the node's data directory, Docker log
and Docker-root sizes, and memory (container and host). Measurement only; it stops the node's clock
after SAMPLE_SECONDS. Runs on a GitHub-hosted runner (uses sudo du / docker), standard library only.
"""

import json
import os
import re
import subprocess
import time
import urllib.request

DATA = os.environ.get("DATA_DIR", "en/mainnet_data")
SECONDS = int(os.environ.get("SAMPLE_SECONDS", "1320"))
EVERY = int(os.environ.get("SAMPLE_EVERY", "60"))
CONTAINER = os.environ.get("EN_CONTAINER", "adi_mainnet_external_node")
OUT = os.environ.get("SAMPLES_OUT", "out/samples.jsonl")
UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def sh(cmd: str, timeout: int = 180) -> str:
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
            urllib.request.Request("http://127.0.0.1:3050", data=body, headers={"Content-Type": "application/json"}), timeout=10
        ) as r:
            return int(json.loads(r.read())["result"], 16)
    except Exception:
        return None


def df(path: str) -> dict | None:
    parts = sh(f"df -B1 --output=size,used,avail {path} 2>/dev/null | tail -n1").split()
    return dict(zip(("size", "used", "avail"), map(int, parts))) if len(parts) == 3 else None


def du_tree(path: str) -> dict[str, int]:
    tree: dict[str, int] = {}
    for line in sh(f"sudo du -x -B1 --max-depth=3 {path} 2>/dev/null", 300).splitlines():
        size, _, name = line.partition("\t")
        if size.isdigit() and int(size) >= 5_000_000:  # ignore < 5 MB entries to keep records small
            tree[name] = int(size)
    return tree


def sample(start: float) -> dict:
    stats = sh(f"docker stats --no-stream --format '{{{{.MemUsage}}}}|{{{{.CPUPerc}}}}' {CONTAINER}").split("|")
    used_limit = stats[0].split("/") if stats and stats[0] else []
    log_path = sh(f"docker inspect -f '{{{{.LogPath}}}}' {CONTAINER}")
    mem = sh("free -b | awk '/Mem:/ {print $3, $7}'").split()
    return {
        "t": round(time.time() - start),
        "utc": time.strftime("%H:%M:%S", time.gmtime()),
        "height": height(),
        "root": df("/"),
        "mnt": df("/mnt"),
        "data_tree": du_tree(DATA),
        "docker_root_bytes": int(sh("sudo du -sxB1 /var/lib/docker 2>/dev/null | cut -f1") or 0),
        "container_log_bytes": int(sh(f"sudo stat -c %s '{log_path}' 2>/dev/null") or 0) if log_path else 0,
        "container_mem_bytes": to_bytes(used_limit[0]) if used_limit else None,
        "container_cpu": stats[1] if len(stats) > 1 else None,
        "cgroup_mem_peak_bytes": int(sh(f"docker exec {CONTAINER} sh -c 'cat /sys/fs/cgroup/memory.peak 2>/dev/null' 2>/dev/null") or 0) or None,
        "host_mem_used_bytes": int(mem[0]) if len(mem) == 2 else None,
        "host_mem_available_bytes": int(mem[1]) if len(mem) == 2 else None,
    }


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    start = time.time()
    with open(OUT, "a", encoding="utf-8") as sink:
        while True:
            tick = time.time()
            record = sample(start)
            sink.write(json.dumps(record, separators=(",", ":")) + "\n")
            sink.flush()
            print(f"t={record['t']:>5}s height={record['height']} used={(record['root'] or {}).get('used', 0) / 1e9:.1f}GB "
                  f"data={record['data_tree'].get(DATA, 0) / 1e9:.2f}GB mem={(record['container_mem_bytes'] or 0) / 1e9:.2f}GB", flush=True)
            if time.time() - start >= SECONDS:
                break
            time.sleep(max(0.0, EVERY - (time.time() - tick)))


if __name__ == "__main__":
    main()
