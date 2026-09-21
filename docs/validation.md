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

## 5. Real server versions A vs B (GitHub Actions)

Workflow: [`.github/workflows/version-experiment.yml`](../.github/workflows/version-experiment.yml),
description in [`experiments/README.md`](../experiments/README.md). Each release runs on its
own GitHub-hosted Ubuntu runner (4 vCPU, 16 GB) with Anvil 1.5.1 as L1; RPC is localhost-only.
Download integrity was verified against the SHA-256 digests GitHub publishes.

| | A | B |
|---|---|---|
| Server release | `v0.20.12` (commit `027ef1f6…`) | `v0.23.0` (commit `610bfa2b…`) |
| Local-chain protocol / config | `v31.0`, `default/config.yaml` (chain ID 506); identical files at both tags | same |
| L2 RPC ready after start | 1.02 s | 1.02 s |
| Server peak memory | about 259 MB | about 272 MB |

Both scans were identical: chain ID 506, `net_version` `0x1fa`, execution version 6 (from
block metadata), the same genesis root, the same capabilities (`zks_getGenesis`,
`zks_getBlockMetadataByNumber`, `zks_getBridgehubContract` supported; the four EraVM batch
methods unsupported) and the same response shapes.

`zkdoctor compare A B`: **one difference**, `client_version` `zksync-os/v0.20.12` →
`zksync-os/v0.23.0` (WARNING, expected by definition). 0 breaking, 11 probes unchanged.

Classification: server/version 1 (the client version); local-chain/genesis 0; capability 0;
schema 0; dynamic/runtime 0; unknown 0.

**This is a neutral result.** It shows the pipeline works on real servers and produces no
false positives from runtime state. It does not show that ZKDoctor can detect a real
compatibility break, because these two releases did not differ on anything V0 probes.
v0.23.0's release notes mention new stable batch RPC methods that V0 does not probe.

### Cross-environment observations (not part of A vs B)

| Item | Public testnet `v0.24.0` | Local `v0.20.12` / `v0.23.0` |
|---|---|---|
| `zks_getGenesis.additional_storage` | array (empty) | object |
| `zks_getGenesis.execution_version` | absent | absent |
| `zks_getBlockMetadataByNumber.execution_version` | absent | present (6) |
| `net_version` | hex | hex |

These are different versions and configs, so they show deployment diversity, not a
regression. Docs say `additional_storage` is an array.

## 6. V0.1.1 fixes

Found by the runs above:

1. `zkdoctor --version` exited 2 with "Missing command" (the option was not eager). Fixed;
   regression test added.
2. ZKS-001 returned **FAIL** for the local servers because `additional_storage` was an object
   where the docs say array. The genesis is usable, so this is now a **WARN**
   ("documented as array, observed object") with the observed shape kept in the evidence.
   Still FAIL: response not an object, `initial_contracts` or `genesis_root` missing or
   malformed, `additional_storage` neither array nor object. Missing `additional_storage`
   is a WARN.
3. `compare` crashed with `TypeError: unhashable type: 'list'` when a field changed between
   an array and an object (for example testnet vs local genesis). Found by the regression
   test for fix 2. Fixed; an array → object change is still reported as BREAKING.

Re-scan of the public testnet with V0.1.1
([`2026-09-21-testnet-v0.1.1/scan.json`](validation/2026-09-21-testnet-v0.1.1/scan.json)):
4 PASS, 3 WARN, 0 FAIL, 4 SKIP, exit 0; compared with the 2026-09-20 scan: no differences.

## 7. Not established

- Detection of a **real** compatibility difference between two versions. The pair tested
  showed none on the probed surface.
- Whether a ZKsync OS v0.4.0 vs v0.5.0 difference is visible: no released server embeds
  v0.5.0 as far as could be determined.
- Behaviour on other ZKsync OS deployments, or on EraVM chains (ZKS-003 to ZKS-006 have only
  been run against fake responses).
- Customer demand, and whether this duplicates official ZKsync tooling.
