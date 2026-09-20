# Validation against a real ZKsync OS node

Date: 2026-09-20. One environment, one day. These are observations, not guarantees.

## Environment

ZKsync OS Developer Preview testnet, as published in the official docs
([network details](https://docs.zksync.io/zksync-network/zksync-os/network-details)):
`https://zksync-os-testnet-alpha.zksync.dev/`, chain ID `8022833`, reporting
`zksync-os/v0.24.0`. That version is newer than the latest published
`zksync-os-server` release (`v0.23.0`), so this environment does not correspond to any
official release tag.

Saved scans: [`validation/2026-09-20-testnet/scan-t0.json`](validation/2026-09-20-testnet/scan-t0.json)
(21:11 UTC) and [`scan-t1.json`](validation/2026-09-20-testnet/scan-t1.json) (21:18 UTC).

## 1. Docs vs node

The first run of the engine (previously tested only against fake nodes) produced one false
FAIL (genesis) and one WARN caused by a wrong parameter type (block metadata). Findings
from the real node:

| Item | Docs | Real node | Effect in ZKDoctor |
|---|---|---|---|
| `net_version` | decimal string | `"0x7a6b31"` (hex) | WARN |
| `zks_getGenesis` | includes `execution_version` | absent; two undocumented fields present (`additional_preimages`, `additional_storage_raw`) | WARN (was a false FAIL, fixed) |
| `zks_getBlockMetadataByNumber` param | `QUANTITY` (hex) | hex rejected (`expected u64`), integer accepted | probe now sends an integer (fixed) |
| `zks_getBlockMetadataByNumber` result | includes `execution_version` | absent | WARN |
| `zks_getBlockDetails`, `zks_L1BatchNumber`, `zks_getL1BatchDetails`, `zks_getL1BatchBlockRange` | documented for EraVM | `-32601 Method not found` | SKIP |
| `zks_getBridgehubContract` | shape not documented | 20-byte address | PASS |

The node sits between two official sources: the docs site lists more `zks_` methods than
the OS server's own `design/rpc.md`, which lists only `zks_getBridgehubContract`. The node
exposes three (`zks_getBridgehubContract`, `zks_getGenesis`,
`zks_getBlockMetadataByNumber`).

Result after the fixes: 4 PASS, 3 WARN, 0 FAIL, 4 SKIP, 0 ERROR. See
[`sources.md`](sources.md) for the full observation table and remaining assumptions.

## 2. Negative control: real vs real, 7 minutes apart

```bash
zkdoctor compare docs/validation/2026-09-20-testnet/scan-t0.json \
                 docs/validation/2026-09-20-testnet/scan-t1.json
```

Between the scans the head block moved `0x7b300` → `0x7b31c`, and the block-metadata
prices changed (`pubdata_price_per_byte` `0x14c666e019` → `0x171bf4c414`, `native_price`
`0x10e8a1` → `0x1312d0`, response hash different, response shape identical). Genesis,
bridgehub and client-version responses were byte-identical.

Result: **0 differences, 11 probes unchanged, exit 0.**

## 3. Positive controls: real scan vs edited copy

These are edits to a copy of the real scan, **not** changes observed on a real upgraded
node. They show the classifier works on real-shaped data. The script used was ad hoc and
is not part of the repository.

| Edit to a copy of `scan-t0.json` | Result |
|---|---|
| client version `v0.24.0` → `v0.25.0` | WARNING |
| chain ID changed | BREAKING |
| `genesis_root` removed from the genesis shape | BREAKING |
| `native_price` retyped string → integer | BREAKING |
| new `execution_version` field in block metadata | NONE |
| `zks_getBridgehubContract` marked unsupported | WARNING |
| `eth_blockNumber` marked unsupported | BREAKING |
| `genesis_root` value changed | WARNING |

All eight matched expectations.

## 4. Other public ZKsync OS environments

Searched official sources for a second public ZKsync OS environment. None found:

- The ZKsync OS FAQ states that a developer-preview testnet is live and
  "Currently there is no public mainnet."
- The docs' network/environment page lists 12 chains, all EraVM; the OS testnet above is
  documented separately.
- The RPC-providers page covers ZKsync Era only.

No endpoint was guessed or probed beyond the one the docs publish.

## 5. Not established

- **A real version A → version B comparison.** Not done. A local run needs Linux (the
  server publishes no Windows binary), Foundry's `anvil`, and each release's
  `local-chains` bundle. It was not attempted on the development machine because the
  system drive had too little free space. Server releases up to `v0.23.0` reference
  ZKsync OS `v0.4.0`; testing OS `v0.5.0` probably needs a newer or source-built server
  (inference from release notes, unverified).
- Behaviour on other ZKsync OS deployments, or on EraVM chains (the EraVM-documented
  probes, ZKS-003 to ZKS-006, have only been exercised against fake responses).
- Customer demand.
