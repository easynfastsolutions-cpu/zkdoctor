# `zkdoctor watch` (experimental)

A validation experiment for one failure mode, not a monitoring product: a node stays reachable
(and may report healthy) while its block height stops advancing and a reference node keeps
moving. Background: [ADI-Stack-EN-Setup-script#21](https://github.com/ADI-Foundation-Labs/ADI-Stack-EN-Setup-script/issues/21)
and [#17](https://github.com/ADI-Foundation-Labs/ADI-Stack-EN-Setup-script/issues/17).

It watches **one target against one reference on the same chain**, read-only, and appends
JSON lines. No alerts, no database, no dashboard.

```bash
zkdoctor watch --rpc <target> --reference <reference> --interval 30s --output watch.jsonl
```

| Option | Default | Meaning |
|---|---|---|
| `--interval` | `30s` | time between polls |
| `--lag-warn-blocks` | `2` | WARN when the target is more than this many blocks behind |
| `--stale-warn` | `60s` | WARN when the target height has been unchanged for longer than this |
| `--stale-fail` | `300s` | FAIL when unchanged for at least this long **while the reference advanced** |
| `--fail-after` | `2` | consecutive FAIL polls before exiting non-zero |
| `--health-url` | none | a status URL **you provide**; never guessed |
| `--output` | none | append JSONL here (earlier records are never rewritten) |
| `--max-polls`, `--duration` | none | bound the run |

## What each poll does

1. `eth_blockNumber` on both nodes (the V0 client and evidence model).
2. Tracks when each height last changed and what the *other* node's height was at that moment.
3. Fetches the block hash at `common_height = min(target, reference)` from both nodes with
   `eth_getBlockByNumber(<height>, false)` and compares them.
4. Optionally GETs the explicit `--health-url` and records the result.

Before the loop it reads `eth_chainId` from both nodes; **different chain IDs are a configuration
error (exit 2)** and nothing else is asked.

## Findings

| Code | Status | When |
|---|---|---|
| `TARGET_LAGGING` | WARN | target more than `--lag-warn-blocks` behind and still moving |
| `TARGET_STALLED` | WARN, then FAIL | target unchanged for `> stale-warn` (FAIL at `>= stale-fail`) **and the reference advanced in that window** |
| `BOTH_STALLED` | WARN (never FAIL) | neither advanced: a quiet chain or both down; says nothing specific about the target |
| `REFERENCE_STALLED` | WARN | reference unchanged for `> stale-warn` while the target advanced |
| `RPC_ERROR` | ERROR | a height could not be read. Never a stall and never a FAIL |
| `HEALTH_CHECK_FALSE_POSITIVE` | FAIL | a provided health check says healthy AND the target has been unchanged for `>= stale-fail` AND the reference advanced. It reports a conflict between two signals, not that the health check is wrong in general |
| `STATE_DIVERGENCE` | FAIL | different block hashes at the same height. Not called a fork |

Poll status is the worst finding: `PASS` (printed `OK`), `WARN`, `FAIL`, or `ERROR`. Peer count is
recorded as `SKIP`/not collected and never affects the status.

**Conservative choices**
- An RPC error resets that node's stale timer, so a stall is only asserted from consecutive
  successful observations. A flaky endpoint can therefore hide a real stall.
- Staleness is measured at poll times, so detection latency is up to one `--interval`.
- On a low-traffic chain blocks can be a minute apart. Use a reference and thresholds suited to
  the chain, and read `BOTH_STALLED` as "cannot tell".
- Hash comparison is at the common height, which may be the tip. On a chain that can reorg a
  mismatch at the tip may be transient; `--fail-after` requires it to persist.

## Exit codes

`0` no sustained FAIL (including a bounded run that ended, or Ctrl-C) · `1` `--fail-after`
consecutive FAIL polls · `2` tool/configuration error (bad arguments, unreachable node at start,
chain ID mismatch, unwritable output, or a bounded run with no usable observation).
A transient timeout, an unsupported optional method, missing peer count and ordinary height
differences never cause a non-zero exit.

## JSONL

Each run appends a `meta` record (schema `zkdoctor-watch/0.1`, both chain IDs and their evidence,
endpoints (redacted) and fingerprints, thresholds, start time, health configuration), then one
`poll` record per observation, then an `end` record with the reason.

A `poll` record holds: `timestamp`, `t` (epoch seconds), `status`, `findings[]`, `blocks_behind`
(`reference - target`), `fail_streak`, and for `target` and `reference`: `height`, `changed`,
`seconds_since_change`, `error`, and the V0 `evidence` object (probe, method, params,
`request_hash`, `response_hash`, `http_status`, `latency_ms`, `result_summary`, timestamp). Also
`state_check` (common height, both hashes, both hash-request evidence), `health` and `peer_count`.

The classification is a pure function of these records: `zkdoctor.watch.replay(path)` recomputes
every poll's status and findings offline, with no network access.

## Not implemented (deliberately)

- **Peer count**: V0 has no probe for it; `net_peerCount` is not in ADI's documented method list.
- **Health discovery**: no ports or paths are guessed. Only a URL you pass is fetched.
- **Block timestamps / freshness**, **continuous alerting**, **multiple targets**.
