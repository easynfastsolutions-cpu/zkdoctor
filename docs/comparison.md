# Comparison semantics

`zkdoctor compare baseline.json target.json` reports what changed between two scans that
matters for compatibility. It does not judge which scan is "right".

## What is compared

1. **Environment identity**: chain ID, net version, client version, detected environment,
   execution version. A value that is unknown (`null`) in either scan is skipped. That is
   reported by the probe instead.
2. **Protocol metadata**: `genesis_root`, `protocol_version`.
3. **Capabilities**: `supported` true ↔ false. `null` on either side is skipped.
4. **Probe response structure**: the recorded `shape` of each probe's result.
5. **Probe status** (PASS/WARN/FAIL/SKIP/ERROR), only when nothing more specific was
   already reported for that probe.

## What is never compared

Block numbers, timestamps, gas/native/pubdata prices, latencies, hashes of live state,
`scan_id`, endpoint fingerprint. These change on a healthy node. Shapes contain no values,
so value drift cannot appear as a difference. Two exceptions are deliberate: the identity
values above, and `genesis_root` in protocol metadata.

Ignored inside shapes: `null` ↔ any type (nullable fields such as not-yet-committed
batch hashes), integer ↔ number, empty arrays (say nothing about element type), anything
deeper than 4 levels.

## Classification

Each difference has a `change` (`ADDED`, `REMOVED`, `CHANGED`) and an `impact`
(`BREAKING`, `WARNING`, `NONE`). Probes with no difference are listed as unchanged.

| Difference | Impact |
|---|---|
| Field present in baseline shape is absent in target | `BREAKING` |
| Field's JSON type changed | `BREAKING` |
| Chain ID changed | `BREAKING` (breaks signature/replay assumptions of every client) |
| `eth_chainId`, `eth_blockNumber` or a `--critical` method: supported → unsupported | `BREAKING` |
| Probe status non-FAIL → FAIL | `BREAKING` |
| Any other method: supported → unsupported | `WARNING` |
| Client / net / execution version, detected environment, `genesis_root`, `protocol_version` changed | `WARNING` |
| A probe hit a transport error (`ERROR`) in either scan | `WARNING` (not comparable) |
| Other status change (e.g. PASS → WARN, PASS → SKIP) | `WARNING` |
| Field added; method unsupported → supported; FAIL → PASS/WARN; capability present in only one scan | `NONE` |

Field-level differences take precedence: if a probe has shape differences, its status
change is not reported separately. If a capability changed, the probe's shape/status is not
also reported.

Exit code: `1` if any difference is `BREAKING`, else `0`; `2` if a scan file cannot be read
or has an unsupported `schema_version`.

## Known blind spots

- Only what the 11 probes call is compared. A method the tool does not probe (for example
  one added in a newer server release) is invisible.
- One sample per probe: intermittent behaviour and rate-limit effects are not detected.
- Structure only: a value that changed meaning but not type is not reported (except the
  identity values listed above).
- Shapes hold the first element of an array only.
