# ZKDoctor: ZKsync Compatibility Doctor

**Status: V0.1 release candidate (`0.1.0`, scan schema `0.1`).** Feature-frozen. Validated
against one real ZKsync OS endpoint (the public Developer Preview testnet); not yet
validated across two real server versions. See [Limitations](#limitations).

A small, deterministic CLI that records what a ZKsync RPC endpoint actually supports and
how it behaves, then compares two recordings:

> After a ZKsync OS version or configuration change, what actually changed or broke?

Its main finding so far is that **documentation, server implementation and a running node
can each disagree** (see [docs/validation.md](docs/validation.md)), so ZKDoctor probes
instead of assuming.

## What it does

1. Discovers the endpoint: chain ID, client version, detected environment
   (`zksync-os` / `zksync-eravm` / `zksync-unknown` / `generic-evm`), execution version.
2. Detects which JSON-RPC methods are supported (capabilities). Read-only.
3. Runs 11 read-only probes (4 generic EVM, 7 `zks_`) and validates response *structure*
   against the documented schemas.
4. Records evidence per probe: request/response SHA-256, HTTP status, RPC error, latency
   and a bounded summary. Never the full response.
5. Saves the scan as versioned JSON.
6. Compares two scans, ignoring dynamic values (block height, timestamps, prices).

It sends no transactions, holds no keys, and does not monitor, alert, score or diagnose.

## Install

Requires Python 3.12+.

```bash
uv sync                    # or: pip install -e .
uv run zkdoctor --help
```

## Scan

```bash
zkdoctor scan --rpc https://zksync-os-testnet-alpha.zksync.dev/ --output scan.json
```

Real output (public ZKsync OS Developer Preview testnet, 2026-09-20; the saved scan is
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

## Compare

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

## Documentation

| Doc | Contents |
|---|---|
| [docs/schema.md](docs/schema.md) | Scan JSON and evidence format, statuses, redaction |
| [docs/comparison.md](docs/comparison.md) | What is compared, classification rules, blind spots |
| [docs/validation.md](docs/validation.md) | Real-testnet validation, docs-vs-node discrepancies, controls |
| [docs/architecture.md](docs/architecture.md) | Module layout and design decisions |
| [docs/sources.md](docs/sources.md) | Official sources, observed behaviour, assumptions |
| [tests/integration/README.md](tests/integration/README.md) | Optional live-node tests and local-node workflow |

## Development

```bash
uv run pytest                                  # 75 tests, localhost only
ZKDOCTOR_INTEGRATION_RPC=https://zksync-os-testnet-alpha.zksync.dev/ \
  uv run pytest tests/integration              # 3 live tests
```

## Limitations

- **One real environment.** Validated against the public ZKsync OS Developer Preview
  testnet only (`zksync-os/v0.24.0`, one day). No second public ZKsync OS environment
  exists to compare it with, and no real version A → B comparison has been run.
  Classification of real upgrades is therefore unproven; the eight "positive controls" in
  the validation record are edits to a saved scan.
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
