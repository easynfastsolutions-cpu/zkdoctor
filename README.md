# ZKDoctor: ZKsync Compatibility Doctor

**Status: v0.2.0.** `scan` and `compare` are the stable core; `watch` is an experimental
addition (see its own section). Every claim below is backed by a real, dated run against
real public infrastructure, linked inline — not a mock.

A small, deterministic CLI that:

- **records** what a ZKsync RPC endpoint actually supports and how it behaves (`scan`),
- **compares** two recordings for compatibility differences (`compare`),
- **watches** one live node against a reference for silent stalls (`watch`, experimental).

It sends no transactions, holds no keys, and does not alert, score, or diagnose. `watch`
polls repeatedly, but is not a monitoring product — see its section.

## `scan` and `compare`

`scan` runs 11 read-only probes against an endpoint (4 generic EVM: `eth_chainId`,
`eth_blockNumber`, `web3_clientVersion`, `net_version`; 7 `zks_` methods), records which
methods are supported, validates response *structure* against the documented ZKsync schemas,
and captures evidence for every probe (request/response SHA-256, HTTP status, RPC error,
latency, a bounded summary — never the full response). `compare` diffs two scans and reports
compatibility-relevant differences, ignoring dynamic values like block height, timestamps,
and prices.

### Real bugs found running this on real infrastructure

- **2026-09-20, public ZKsync OS Developer Preview testnet** (`zksync-os-testnet-alpha.zksync.dev`,
  chain ID `8022833`, client `zksync-os/v0.24.0`): the documented schema and the real node
  disagreed in four places — `net_version` is hex, not the documented decimal string;
  `zks_getGenesis` is missing the documented `execution_version` field and has undocumented
  ones (`additional_preimages`, `additional_storage_raw`); `zks_getBlockMetadataByNumber`
  rejects the documented hex-string block parameter and requires a JSON integer instead.
  Full record: [docs/validation.md](docs/validation.md), saved scans in
  [`docs/validation/2026-09-20-testnet/`](docs/validation/2026-09-20-testnet/).
- **2026-09-21, two real `zksync-os-server` releases (`v0.20.12`, `v0.23.0`) run side by side
  on GitHub Actions**
  ([run 35568950248](https://github.com/easynfastsolutions-cpu/zkdoctor/actions/runs/35568950248),
  then fixed and re-verified in
  [run 35569606266](https://github.com/easynfastsolutions-cpu/zkdoctor/actions/runs/35569606266)):
  this run found two real bugs in ZKDoctor itself — `zkdoctor --version` exited 2 with
  "Missing command" instead of printing the version (the option wasn't eager), and the
  genesis-schema check FAILed a real server's response because `additional_storage` was an
  object where the docs say array, when the response was actually fine (softened to WARN,
  with the observed shape kept in evidence). Both fixed in what became v0.1.1. The A/B
  comparison itself found only the expected client-version difference between the two
  releases — 0 breaking, 11 probes unchanged — a neutral result, not a caught compatibility
  break.
- **2026-09-21, ADI mainnet and testnet public RPCs** (chain IDs `36900` / `99999`, client
  `zksync-os/v0.21.1`): reconfirmed the same `net_version`-is-hex and missing-`execution_version`
  pattern on a third, unrelated deployment — this is a real, recurring gap between the ZKsync
  OS docs and multiple independent nodes, not a one-off.

### Usage

```bash
zkdoctor scan --rpc https://zksync-os-testnet-alpha.zksync.dev/ --output scan.json
```

Real output (2026-09-20; saved scan:
[`docs/validation/2026-09-20-testnet/scan-t0.json`](docs/validation/2026-09-20-testnet/scan-t0.json)):

```text
ZKsync Compatibility Doctor
────────────────────────────────────────

Environment
  Endpoint:          https://zksync-os-testnet-alpha.zksync.dev/
  Chain ID:          8022833
  Client:            zksync-os/v0.24.0
  Detected:          zksync-os
  Execution version: -
    basis: zks_getGenesis supported (documented as ZKsync OS only)
    basis: zks_getBlockMetadataByNumber supported (documented as ZKsync OS only)

Discovery
  Generic RPC       3/4 PASS
  ZKsync RPC        3/7 supported

Checks
  ✓ RPC-001 chain ID
  ✓ RPC-002 block number
  ✓ RPC-003 client version
  ⚠ RPC-004 net version - response matches schema with deviations
      net_version is not a decimal string: '0x7a6b31'
  ⚠ ZKS-001 genesis - response matches schema with deviations
      documented field 'execution_version' is absent
  ⚠ ZKS-002 block metadata - response matches schema with deviations
      documented field 'execution_version' is absent
  - ZKS-003 block details - unsupported
  - ZKS-004 L1 batch number - unsupported
  - ZKS-005 L1 batch block range - skipped: needs a value from ZKS-004
  - ZKS-006 L1 batch details - skipped: needs a value from ZKS-004
  ✓ ZKS-007 bridgehub contract

Summary
  PASS: 4   WARN: 3   FAIL: 0   SKIP: 4   ERROR: 0
```

The three WARNs are places where the node differs from the docs; they are not failures.
Terminals that cannot print Unicode get ASCII markers.

Options: `--output/-o FILE`, `--timeout SECONDS` (default 10), `--require METHOD`
(repeatable; a missing method becomes a `FAIL`), `--json` (JSON to stdout instead of the
report). Credentials in the URL are redacted from everything printed or stored
([details](docs/schema.md#redaction)).

```bash
zkdoctor scan --rpc http://localhost:3050 --output baseline.json    # before the change
zkdoctor scan --rpc http://localhost:3050 --output target.json      # after the change
zkdoctor compare baseline.json target.json --output comparison.json
```

Two real scans of the testnet seven minutes apart, while the block height and prices moved:

```text
$ zkdoctor compare scan-t0.json scan-t1.json
No compatibility differences found.

UNCHANGED
  11 probes
```

Example of a real difference (from the test suite's fake nodes: a genesis response that
lost a field, plus a client-version bump):

```text
BREAKING

ZKS-001
  field removed: execution_version
  was: integer

WARNING

environment
  client version changed
  zksync-os/0.4.0 -> zksync-os/0.5.0
```

Options: `--output/-o FILE`, `--critical METHOD` (repeatable; extra methods whose loss is
`BREAKING`), `--json`. Classification rules: [docs/comparison.md](docs/comparison.md).

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | No breaking/failed findings |
| 1 | `scan`: a probe `FAIL`ed. `compare`: at least one `BREAKING` difference |
| 2 | Usage/tool error: bad arguments, unreadable scan file, unreachable endpoint, or a probe hit a transport error (`ERROR`) |

An unreachable endpoint is exit 2, never a compatibility failure.

## `watch` (experimental)

`watch` polls a target node against a reference node on the same chain, read-only, and
appends one JSON record per poll. It exists to answer one narrow question: **can a
lightweight, black-box scanner detect a node that is silently stalled while still reporting
itself healthy** — the failure pattern in a real public incident,
[ADI-Stack-EN-Setup-script#21](https://github.com/ADI-Foundation-Labs/ADI-Stack-EN-Setup-script/issues/21):
an external node pinned to an old image (`v0.20.12-b1`) fell behind mainnet (already on
`v0.21.1`) and stopped applying blocks entirely, while its status endpoint kept answering
`{"healthy": true}` and its RPC kept serving the stale head. A related report,
[issue #17](https://github.com/ADI-Foundation-Labs/ADI-Stack-EN-Setup-script/issues/17),
describes the same shape: a node frozen for ~24h while `healthy: true` the entire time.

It is not a monitoring product: one target, one reference, no dashboard, no alerting, no
database. See [docs/watch.md](docs/watch.md) for the full finding model.

### The honest result: validated, not yet a catch

Two full live-node reproduction attempts were run: an ADI external node pinned to the exact
`v0.20.12-b1` image from the incident, started fresh from genesis on a GitHub-hosted runner,
watched with `zkdoctor watch` against ADI's real public mainnet RPC
(`https://rpc.adifoundation.ai/`), chain ID confirmed matching (`36900`) before any comparison.

- **Attempt 1** ([run 35577380179](https://github.com/easynfastsolutions-cpu/zkdoctor/actions/runs/35577380179),
  2026-09-21): synced past the incident's exact stall block (1,253,580) with **282/282** real
  block-hash agreements against the live reference, then the container died — not from the
  incident condition, but from the runner's disk filling up (diagnosed precisely: ~76 GiB of
  chain data + ~10 GiB of uncapped Docker logs). This produced 269 consecutive `RPC_ERROR`
  polls (2h18m) with exit code 0 — a real gap in `watch` itself (nothing escalated an error
  that never stopped), fixed by adding the `TARGET_UNREACHABLE_SUSTAINED` finding.
- **Attempt 2** ([run 35615807966](https://github.com/easynfastsolutions-cpu/zkdoctor/actions/runs/35615807966),
  2026-09-21, after fixing the disk/log issue and adding explicit exit-code/OOM diagnostics):
  ran the full ~5.6 hours, reached the live chain tip (height climbed from 9 to 1,390,499,
  ~69 blocks/s average), and the container never died — confirmed directly (`docker inspect`:
  `Running=true`, `ExitCode=0`, `OOMKilled=false`, zero kernel OOM lines in `dmesg`). **652/652**
  state-hash checks passed. One transient `TARGET_STALLED` WARNing fired at 19:35:12 UTC
  (target height unchanged for 123s) and self-resolved the next poll — health correctly stayed
  `healthy` throughout, matching the near-instant real recovery, so `HEALTH_CHECK_FALSE_POSITIVE`
  correctly never fired. Zero `STATE_DIVERGENCE`, zero sustained failures.

**Combined: 1,203 real polls (551 + 652) across the two reproduction attempts, zero false
positives** — every WARN or FAIL raised was justified by what was actually happening, and
every all-clear was correct.

**What this does and doesn't show.** The detection logic is now validated against real,
independently-run infrastructure, not just mocks — it correctly told a syncing-but-behind
node apart from a stalled one, correctly withheld the health-mismatch finding when health and
reality agreed, and correctly refused to call a merely-slow RPC error a stall. But **the
specific incident condition — a fully-synced, live-following node silently freezing while its
own health endpoint still reports healthy — was never observed live in either ~5.5-hour
window.** This is reported plainly as **inconclusive**, not as a win: the observation windows
may simply have been too short relative to however long the original incident's node sat
frozen before anyone noticed it.

### Usage

```bash
zkdoctor watch --rpc http://127.0.0.1:3050 --reference https://rpc.adifoundation.ai/ \
  --interval 30s --health-url http://127.0.0.1:3071/status/health --output watch.jsonl
```

The one real `TARGET_STALLED` finding from attempt 2 (trimmed from the actual JSONL record,
`watch-adi-repro.jsonl`, poll 533):

```json
{
  "seq": 533,
  "timestamp": "2026-09-21T19:35:12.217Z",
  "status": "WARN",
  "findings": [{
    "code": "TARGET_STALLED",
    "status": "WARN",
    "message": "target height 1389412 unchanged for 123s while reference advanced 1 blocks (now 1 behind)"
  }],
  "target": {"height": 1389412, "seconds_since_change": 123.44},
  "reference": {"height": 1389413},
  "health": {"status": "healthy"}
}
```

Options: `--lag-warn-blocks` (default 2), `--stale-warn`/`--stale-fail` (default 60s/300s),
`--fail-after` (default 2), `--unreachable-after` (default 10), `--health-url` (never guessed
— only fetched if you provide it). Full finding model, exit codes, and JSONL schema:
[docs/watch.md](docs/watch.md).

## Install

Not yet published to PyPI. Requires Python 3.12+.

```bash
pip install git+https://github.com/easynfastsolutions-cpu/zkdoctor.git
zkdoctor --help
```

For development:

```bash
git clone https://github.com/easynfastsolutions-cpu/zkdoctor.git && cd zkdoctor
uv sync                    # or: pip install -e .
uv run zkdoctor --help
```

## Documentation

| Doc | Contents |
|---|---|
| [docs/schema.md](docs/schema.md) | Scan JSON and evidence format, statuses, redaction |
| [docs/comparison.md](docs/comparison.md) | What is compared, classification rules, blind spots |
| [docs/validation.md](docs/validation.md) | Real-testnet validation, docs-vs-node discrepancies, controls |
| [docs/watch.md](docs/watch.md) | `watch` finding model, thresholds, JSONL schema |
| [docs/architecture.md](docs/architecture.md) | Module layout and design decisions |
| [docs/sources.md](docs/sources.md) | Official sources, observed behaviour, assumptions |
| [tests/integration/README.md](tests/integration/README.md) | Optional live-node tests and local-node workflow |

## Development

```bash
uv run pytest                                  # 127 tests (43 for watch), localhost only, 3 skipped
ZKDOCTOR_INTEGRATION_RPC=https://zksync-os-testnet-alpha.zksync.dev/ \
  uv run pytest tests/integration              # 3 live tests
```

## Limitations

- **A real A → B comparison has run, but it found nothing to classify.** Server `v0.20.12`
  vs `v0.23.0` (same `v31.0` local chain) differed only in the client-version string
  ([details](docs/validation.md)). Either the versions behave the same for the 11 probes,
  or the probes are too shallow to see the difference (v0.23.0 adds RPC methods that are
  not probed). Detection of a real compatibility break is therefore still unproven; the
  eight "positive controls" in the validation record are edits to a saved scan.
- **Deployments differ from each other and from the docs.** For example
  `zks_getGenesis.additional_storage` is an array on the public testnet and an object on
  the local releases; ZKDoctor reports this as a WARN with the shape kept in the evidence.
- **EraVM probes untested on a real node.** ZKS-003 to ZKS-006 (documented for EraVM) have
  only been run against fake responses; the OS testnet does not expose them.
- **Only probed methods are seen.** A method that appears in a newer server and is not one
  of the 11 probes is invisible.
- ZKsync OS RPC is documented as unstable, and official sources disagree with each other
  and with the node. Hence "probe, never assume".
- Environment detection is inferred from method availability and the client-version string.
- Compares response *structure* only; it does not verify values, proofs, L1 state or
  behaviour under load. One sample per probe, so no intermittent-failure detection.
- No transaction-based or write probes, by design.
- **`watch` has not caught the failure mode it was built for.** See the honest result above —
  the detection logic is validated on real infrastructure, but the specific silent-stall
  condition has not been observed live. Two ~5.5-hour windows is not long enough to rule it
  out as rare or conditional (e.g. tied to a specific version-skew event) rather than common.
- **`watch` peer count is not collected** — no existing probe for it, and it is not in ADI's
  documented method list. It would only ever be corroborating evidence, never an independent
  trigger.
- **`watch` health checks are never discovered** — only a URL explicitly provided by the
  operator is fetched. No port or path is guessed.
