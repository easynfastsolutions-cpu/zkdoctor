# Architecture

```text
CLI (cli.py)
  ↓
RPC client (client.py)        read-only JSON-RPC over httpx; redaction; typed errors
  ↓
Discovery (discovery.py)      runs each probe's call once, in dependency order;
  ↓                           derives capabilities + environment identity
Probes (probes.py)            evaluate the recorded observations against documented schemas
  ↓
Evidence (evidence.py)        hashes, bounded summaries, shapes; scan save/load
  ↓
Comparison (compare.py)       baseline vs target, structural + identity only
  ↓
Reports (report.py)           terminal (rich) and JSON, both from models.py objects
```

## Modules

| Module | Responsibility |
|---|---|
| `models.py` | pydantic models: `RpcObservation`, `Evidence`, `ProbeResult`, `Capability`, `Environment`, `Scan`, `Difference`, `Comparison`. The scan JSON *is* `Scan`. |
| `client.py` | `RpcClient.call()` returns an observation for any well-formed JSON-RPC reply (incl. error replies). `RpcTransportError` = no HTTP reply; `RpcProtocolError` = reply was not JSON-RPC. Refuses non-read-only methods (`eth_send*`, `eth_sign*`, `personal_*`, `admin_*`, `debug_*`, ...). `redact_url` / `scrub` keep secrets out of everything stored or printed. |
| `discovery.py` | `discover()` calls each probe method with parameters built from earlier results (e.g. block number from `eth_blockNumber`), never fixed values. Builds capabilities (`supported` true / false / unknown) and `Environment`. Aborts with `DiscoveryAborted` only if the first call cannot reach the endpoint. |
| `probes.py` | `ProbeSpec` table (id, method, params builder, schema validator, flags), `evaluate()` (status logic), `run_scan()`. |
| `evidence.py` | `build_evidence`, `summarize_result` (type, shape, size, bounded excerpt), `shape_of`, `save_scan` / `load_scan`. |
| `compare.py` | `compare_scans()`; classification rules live in `IDENTITY_RULES`, `DEFAULT_CRITICAL`, `_probe_differences`. |
| `report.py` | `render_scan`, `render_comparison`. |

## Key design decisions

- **Call once, evaluate separately.** Discovery makes every network call; probes only judge
  recorded observations. A scan is therefore a fixed set of requests, and evaluation is
  pure and unit-testable.
- **Capability ≠ health.** "Method not found" (`-32601` or matching message) means
  `supported: false` and the probe is `SKIP`. It becomes `FAIL` only when the probe is
  `required` (`eth_chainId`, `eth_blockNumber`) or the user passes `--require`. On a node
  detected as ZKsync OS, a missing OS-documented method is `WARN`.
- **Schema tolerance.** Per method: `required` documented fields (missing/mistyped →
  `FAIL`), `optional` documented fields (mistyped → `WARN`, missing ignored), unknown
  fields ignored.
- **Shapes, not values, for comparison.** Evidence stores a data-free `shape` of each
  result. `compare` diffs shapes (removed / added / retyped fields; `null` flips and
  int↔float ignored) so dynamic values can never cause a difference.
- **Version-sensitive knowledge is in one place.** Method names and schemas are in the
  `PROBES` table; the OS-only / EraVM method lists are constants in `discovery.py`; impact
  rules are constants in `compare.py`.
- **Exit codes separate tool errors (2) from compatibility findings (1).**

## Adding a probe

Add a `ProbeSpec` to `PROBES` (dependencies must appear earlier in the list), with a
validator built from `check_object` / `check_scalar`. Add a fake response to
`tests/conftest.py` and a test in `tests/test_probes.py`.
