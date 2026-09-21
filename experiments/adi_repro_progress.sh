#!/usr/bin/env bash
# Wait up to N minutes (or until `zkdoctor watch` exits), then publish a progress annotation.
# usage: adi_repro_progress.sh <label> <max-minutes>      (needs WATCH_PID in the environment)
set -u
label="$1"; max_minutes="$2"
EN=adi_mainnet_external_node

for ((i = 0; i < max_minutes * 2; i++)); do
  kill -0 "${WATCH_PID:-0}" 2>/dev/null || break
  sleep 30
done

strip() { sed 's/\x1b\[[0-9;]*m//g'; }
{
  running=no; kill -0 "${WATCH_PID:-0}" 2>/dev/null && running=yes
  echo "utc=$(date -u +%H:%M:%S) watch_running=$running en_container=$(docker inspect -f '{{.State.Status}} oom_killed={{.State.OOMKilled}} exit={{.State.ExitCode}}' "$EN" 2>/dev/null || echo gone)"
  echo "--- last watch lines"
  tail -n 4 logs/watch.txt 2>/dev/null | cut -c1-200
  echo "--- monitor (last sample)"
  tail -n 1 out/monitor.jsonl 2>/dev/null | cut -c1-700
  echo "--- EN log: ERROR / WARN / panic lines (last 5)"
  docker logs --tail 3000 "$EN" 2>&1 | strip | sed "s/${EN_KEY:-__none__}/***/g" \
    | grep -E ' (ERROR|WARN) |panic|FATAL|Killed|out of memory|unsupported|incompatible' | tail -n 5 | cut -c1-240
  echo "--- EN health endpoint: $(curl -s -m 5 http://127.0.0.1:3071/status/health || echo unreachable)"
  echo "--- EN rpc head: $(curl -s -m 5 -X POST -H 'Content-Type: application/json' --data '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}' http://127.0.0.1:3050 || echo unreachable)"
} > "logs/progress-$label.txt" 2>&1

msg="$(cat "logs/progress-$label.txt")"
msg="${msg//'%'/'%25'}"; msg="${msg//$'\r'/'%0D'}"; msg="${msg//$'\n'/'%0A'}"
echo "::notice title=progress-$label::${msg:0:3900}"
