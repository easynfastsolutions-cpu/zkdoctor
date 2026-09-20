# Sources

Consulted on 2026-09-20/21. Everything below is an official ZKsync or Matter Labs source.
Content was read through a summarising fetch tool, so field names/types were taken from
those summaries; re-verify against the pages before relying on them.

## Official sources consulted

| Source | Used for |
|---|---|
| https://docs.zksync.io/zksync-protocol/api/zks-rpc | Method list; parameters and response fields for `zks_getGenesis`, `zks_getBlockMetadataByNumber`, `zks_getBlockDetails`, `zks_L1BatchNumber`, `zks_getL1BatchBlockRange`, `zks_getL1BatchDetails`; "ZKsync OS only" vs "Available for EraVM chains" labels |
| https://docs.zksync.io/zksync-protocol/api/ethereum-rpc | Result types of `eth_chainId`, `eth_blockNumber`, `web3_clientVersion`, `net_version`; note that ZKsync OS endpoints "are still under development, and may be unstable" |
| https://docs.zksync.io/zksync-network/zksync-os | ZKsync OS overview (confirmed it does not document the RPC surface) |
| https://github.com/matter-labs/zksync-os-server | Server overview; docs layout |
| https://raw.githubusercontent.com/matter-labs/zksync-os-server/main/docs/src/design/rpc.md | Which RPC namespaces/methods the OS server supports; block-tag semantics |
| https://raw.githubusercontent.com/matter-labs/zksync-os-server/main/docs/src/setup/local_run.md | Local run (`run_local.sh`, Anvil, port 3050) for the integration workflow |
| https://raw.githubusercontent.com/matter-labs/zksync-os-server/main/docs/src/setup/exposed_ports.md | Default JSON-RPC port 3050 |
| https://raw.githubusercontent.com/matter-labs/zksync-os-server/main/docs/src/setup/prerequisites.md | Rust + Foundry prerequisites |
| https://github.com/matter-labs/zksync-os-integration-tests | How the official integration suite runs (Anvil L1, deployed contracts, in-process servers, `cargo test -p tests --release`) |

## Stable documented interfaces (used as-is)

- `eth_chainId`, `eth_blockNumber` → hex `QUANTITY`; `web3_clientVersion` → string;
  `net_version` → string of the chain ID.
- `zks_L1BatchNumber` → hex `QUANTITY`; `zks_getL1BatchBlockRange(batch)` → `[begin, end]`
  hex strings; `zks_getL1BatchDetails(batch)` and `zks_getBlockDetails(block)` → the
  objects listed in the zks-rpc page. These are documented for EraVM chains.

## Currently unstable ZKsync OS interfaces

The docs state ZKsync OS endpoints "are still under development, and may be unstable."

- `zks_getGenesis` → `initial_contracts`, `additional_storage`, `execution_version`,
  `genesis_root`. Documented "ZKsync OS only".
- `zks_getBlockMetadataByNumber(block)` → `pubdata_price_per_byte`, `native_price`,
  `execution_version`. Documented "ZKsync OS only".
- `zks_getBridgehubContract` → response structure not confirmed (see assumptions).

## Observed behaviour of a real node (first real-node run)

Endpoint: the public ZKsync OS Developer Preview testnet published on
https://docs.zksync.io/zksync-network/zksync-os/network-details
(`https://zksync-os-testnet-alpha.zksync.dev/`, chain ID 8022833).
Observed 2026-09-20, `web3_clientVersion` = `zksync-os/v0.24.0`. One endpoint, one date:
these are observations, not guarantees. Saved scans and the full validation record:
[`validation.md`](validation.md), [`validation/2026-09-20-testnet/`](validation/2026-09-20-testnet/).

| Item | Documented | Observed | Result |
|---|---|---|---|
| `eth_chainId`, `eth_blockNumber` | hex quantity | hex quantity | matches |
| `web3_clientVersion` | string | `zksync-os/v0.24.0` (so the `zksync-os` heuristic held) | matches |
| `net_version` | decimal string (`"324"`) | `"0x7a6b31"` (hex) | **docs ≠ node**, WARN |
| `zks_getGenesis` fields | `initial_contracts`, `additional_storage`, `execution_version`, `genesis_root` | first, second and last only; **no `execution_version`**; extra undocumented `additional_preimages`, `additional_storage_raw` | **docs ≠ node**, WARN |
| `zks_getBlockMetadataByNumber` param | `QUANTITY` | hex string rejected (`Invalid params ... expected u64`); JSON **integer** accepted; `"latest"` rejected | **docs ≠ node** (probe fixed to send an integer) |
| `zks_getBlockMetadataByNumber` result | `pubdata_price_per_byte`, `native_price`, `execution_version` | first two only | **docs ≠ node**, WARN |
| `zks_getBridgehubContract` | response shape unconfirmed | 20-byte hex address | matches assumption |
| `zks_getBlockDetails`, `zks_L1BatchNumber`, `zks_getL1BatchDetails`, `zks_getL1BatchBlockRange` | documented "available for EraVM chains" | `-32601 Method not found` | consistent with the EraVM label |

### The two official sources disagree, and the node sits between them

`zks-rpc` on docs.zksync.io lists `zks_getGenesis`, `zks_getBlockMetadataByNumber` and the
EraVM L1-batch methods. The OS server's `design/rpc.md` says its `zks_` namespace is
"minimal" and supports only `zks_getBridgehubContract`. The real node exposes three
methods: `zks_getBridgehubContract` plus the two OS-only ones. So `rpc.md` was stale
relative to this node, and the docs site is closer but still differs in fields, parameter
types and `net_version` format. This is why V0 never assumes a `zks_` method exists.

## Interfaces still unstable / not confirmed

- `execution_version` is documented but absent from both OS responses on v0.24.0; it may
  return, or the docs may be ahead of the server. `compare` will report it as a field
  added/removed when it changes.
- The relation between `zksync-os/v0.24.0` (server, from `web3_clientVersion`) and the
  ZKsync OS release tags (e.g. v0.4.0, v0.5.0) was not verified.

## Implementation assumptions (still not confirmed by an official source)

1. Method-not-found is detected by JSON-RPC code `-32601` or a message matching
   "method ... not found/not supported/does not exist/not available". The real node used
   `-32601`; other servers' wordings are untested.
2. `zks_getBridgehubContract` returns a string, normally a 20-byte hex address; anything
   else gives `WARN`.
3. Numeric fields documented as `uint32`/`uint256` are accepted as a non-negative JSON
   integer, a `0x` hex string, or a decimal string. The real node sent `0x` strings for
   prices; a change between forms is reported by `compare` as a type change.
4. `zks_getBlockDetails` / `zks_getL1BatchDetails` are called with a JSON integer (their
   documented `uint32`); untested on a real node because both are unsupported there.
5. Only `number` and `timestamp` (block details) / `number` (batch details) are required.
6. The `zksync-os` substring of `web3_clientVersion` identifies ZKsync OS (held on v0.24.0).
7. Environment classification is inferred from which documented methods respond.
8. "Latest" is approximated by `eth_blockNumber`. It worked for block metadata on v0.24.0.
