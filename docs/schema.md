# Scan file and evidence format (schema_version 0.1)

The scan JSON is the canonical output. The terminal report is rendered from the same
objects. Loading rejects a different major `schema_version`, so a future 1.x cannot be
silently misread as 0.x.

## Top level

| Field | Meaning |
|---|---|
| `schema_version` | `"0.1"` |
| `tool_version` | ZKDoctor version that produced the file |
| `scan_id` | random hex id (not deterministic) |
| `timestamp` | UTC ISO-8601, millisecond precision, `Z` suffix |
| `environment` | identity, see below |
| `capabilities` | `{ method: {method, supported, probe_id, reason} }` |
| `results` | one `ProbeResult` per probe, ordered by probe id |
| `summary` | `{pass, warn, fail, skip, error}` counts. No scores by design |

Key order is fixed. Only `scan_id`, timestamps, `latency_ms` and (on a live chain) values
inside `excerpt` vary between runs of an unchanged endpoint.

## `environment`

`endpoint` (credentials redacted), `endpoint_fingerprint` (first 16 hex of the SHA-256 of
the redacted endpoint), `chain_id` (int), `net_version`, `client_version`,
`detected_execution_environment` (`zksync-os` | `zksync-eravm` | `zksync-unknown` |
`generic-evm` | `unknown`), `detection_basis` (list of the observations that led to it),
`execution_version` (int or `null` when the node does not report one),
`protocol_metadata` (`genesis_root`, `protocol_version` when available).

`detected_execution_environment` is inferred from which documented methods respond and from
the client-version string; it is a heuristic, not a declaration by the node.

## `capabilities`

`supported` is `true` (method exists, even if the call returned an error or bad data),
`false` (JSON-RPC `-32601` or a "method not found"-style message) or `null` (could not be
determined: a transport error, or the parameter this call needs came from a method that
is unsupported). `reason` explains `false`/`null`.

## `results[]` (ProbeResult)

`probe_id`, `name`, `category` (`generic` | `zksync`), `status`, `severity`, `summary`,
`details` (list of strings), `evidence`.

| Status | Meaning | Severity |
|---|---|---|
| `PASS` | response matches the documented schema | `INFO` |
| `SKIP` | method unsupported, or a dependency was unavailable | `INFO` |
| `WARN` | supported, but the response deviates from docs, or an RPC error / null was returned | `WARNING` |
| `FAIL` | required method missing, or a required documented field is missing or mistyped | `CRITICAL` |
| `ERROR` | no usable response (timeout, connection, non-JSON-RPC reply) | `CRITICAL` |

A missing method is `FAIL` only for `eth_chainId` / `eth_blockNumber` or when the user
passes `--require METHOD`. On a node detected as ZKsync OS, a missing method that the docs
list as ZKsync-OS-only is a `WARN`; otherwise `SKIP`.

Field tiers per schema: **required** (missing or wrong type → `FAIL`), **documented**
(absent or wrong type → `WARN`), **optional** (wrong type → `WARN`, absence ignored).
Fields the docs do not mention are always ignored.

`zks_getGenesis` (V0.1.1): `initial_contracts` (array) and `genesis_root` (32-byte hash) are
required. `additional_storage` is documented as an array; an object is accepted as a `WARN`
("documented as array, observed object") because real local servers return one, and the
observed shape stays in the evidence. Missing → `WARN`; any other type → `FAIL`.

## `evidence`

One record per probe that made a call. Enough to reproduce the call and to recognise the
response, without storing it.

| Field | Meaning |
|---|---|
| `timestamp` | when the request was sent |
| `probe_id`, `method`, `params` | the exact request. Parameters that depend on earlier calls are the real values used |
| `depends_on` | probe ids whose results produced `params` |
| `request_hash` | SHA-256 of canonical JSON `{method, params}` |
| `response_hash` | SHA-256 of canonical JSON `{result, error}`; `null` if there was no response |
| `http_status`, `rpc_error` | as received |
| `error` | transport/protocol failure text (secrets scrubbed), when there was no response |
| `result_summary` | `type`, `shape`, `size_bytes`, bounded `excerpt`, and `keys`/`length` |
| `latency_ms` | request round trip |

`shape` is the structure of the result with no data: scalar type names, objects as
`{key: shape}`, arrays as a one-element list holding the shape of the first item,
depth-limited to 4 levels (`"..."` beyond). `excerpt` holds at most 20 scalar fields, each
clipped to 80 characters, so a 48 KB genesis is recorded as a hash, a shape and a size.

`comparison` output (`compare --output`) has `baseline` / `target` metadata,
`differences[]` (`subject`, `path`, `change`, `impact`, `message`, `baseline`, `target`),
`unchanged_probes` and a `summary`. See [comparison.md](comparison.md).

## Redaction

Stored and printed URLs drop `user:password@`, blank query values whose key contains
`key|token|secret|auth|pass|sig`, and replace path segments that look like API keys
(24+ characters of `[A-Za-z0-9_-]`) with `***`. Transport error messages are scrubbed of
the same values before they are stored.
