#!/usr/bin/env bash
# Death / final diagnostics for the external-node container. Read-only: it only inspects and prints.
# usage: adi_repro_diag.sh <label>     -> out/diag-<label>.txt
set -u
label="$1"
c="${EN_CONTAINER:-adi_mainnet_external_node}"
mkdir -p out
strip() { sed 's/\x1b\[[0-9;]*m//g' | sed "s/${EN_KEY:-__none__}/***/g"; }
{
  echo "== utc $(date -u +%FT%TZ)  label=$label"
  echo "== container state (docker inspect .State)"
  docker inspect -f '{{json .State}}' "$c" 2>&1
  echo "== restart count / limits / log config"
  docker inspect -f 'restarts={{.RestartCount}} memory_limit={{.HostConfig.Memory}} logconfig={{json .HostConfig.LogConfig}}' "$c" 2>&1
  echo "== docker events (container die / oom / kill / stop)"
  grep -E ' (die|oom|kill|stop) ' logs/docker-events.txt 2>/dev/null | tail -n 20
  echo "== docker logs --tail 150"
  docker logs --tail 150 "$c" 2>&1 | strip | cut -c1-300
  echo "== dmesg: memory / disk / kill lines"
  sudo dmesg -T 2>&1 | grep -iE 'out of memory|oom|killed process|no space|I/O error|EXT4-fs (error|warning)|blocked for more than|segfault' | tail -n 60
  echo "== dmesg tail 60"
  sudo dmesg -T 2>&1 | tail -n 60
  echo "== journal (kernel) oom lines"
  sudo journalctl -k --no-pager -n 500 2>&1 | grep -iE 'oom|killed process|out of memory' | tail -n 30
  echo "== df"; df -h / /mnt 2>&1
  echo "== memory"; free -m; echo; ps aux --sort=-%mem 2>&1 | head -n 8 | cut -c1-200
} > "out/diag-$label.txt" 2>&1
echo "wrote out/diag-$label.txt ($(wc -c < "out/diag-$label.txt") bytes)"
