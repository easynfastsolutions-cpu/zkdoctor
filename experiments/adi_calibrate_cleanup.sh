#!/usr/bin/env bash
# Measure, then remove, preinstalled toolchains on a GITHUB-HOSTED RUNNER, recording the free-disk gain of each.
# Refuses to run anywhere else (it is never meant to touch a developer machine).
set -u
if [ "${GITHUB_ACTIONS:-}" != "true" ] || [ "${RUNNER_ENVIRONMENT:-}" != "github-hosted" ]; then
  echo "refusing to run: not a GitHub-hosted runner" >&2
  exit 1
fi
mkdir -p out
avail() { df -B1 --output=avail / | tail -n1 | tr -d ' '; }
: > out/cleanup.tsv
read -r size used avl < <(df -B1 --output=size,used,avail / | tail -n1); echo "$size $used $avl" > out/df-before.txt

shopt -s nullglob
candidates=(
  /usr/share/dotnet /usr/local/lib/android /opt/ghc /usr/local/.ghcup /opt/hostedtoolcache
  /usr/local/share/boost /usr/share/swift /usr/local/share/powershell /usr/local/share/chromium
  /opt/microsoft /opt/az /usr/lib/google-cloud-sdk /usr/lib/jvm /usr/local/graalvm /usr/local/lib/heroku
  /usr/share/miniconda /usr/local/aws-cli /usr/local/aws-sam-cli /opt/pipx
  /usr/local/julia* /usr/share/gradle*
)
for d in "${candidates[@]}"; do
  [ -e "$d" ] || continue
  bytes="$(sudo du -sxB1 "$d" 2>/dev/null | cut -f1)"
  before="$(avail)"
  sudo rm -rf "$d"
  after="$(avail)"
  printf '%s\t%s\t%s\n' "$d" "${bytes:-0}" "$((after - before))" >> out/cleanup.tsv
done

# preinstalled Docker images (our own image is pulled afterwards)
docker system df > out/docker-df-before.txt 2>&1 || true
before="$(avail)"; docker image prune -a -f > /dev/null 2>&1 || true; after="$(avail)"
printf '%s\t%s\t%s\n' "docker preinstalled images" "0" "$((after - before))" >> out/cleanup.tsv

# package caches
before="$(avail)"; sudo apt-get clean > /dev/null 2>&1 || true; sudo rm -rf /var/lib/apt/lists/* 2>/dev/null || true; after="$(avail)"
printf '%s\t%s\t%s\n' "apt caches" "0" "$((after - before))" >> out/cleanup.tsv

read -r size used avl < <(df -B1 --output=size,used,avail / | tail -n1); echo "$size $used $avl" > out/df-after.txt
{ echo "== df after cleanup"; df -h / /mnt 2>&1; echo; echo "== swap"; swapon --show; } | tee out/after-cleanup.txt
