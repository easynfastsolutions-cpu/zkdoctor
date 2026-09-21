# Experiment: real server version A vs B

Purpose: check whether the unmodified ZKDoctor V0.1 can detect a difference between two
genuinely different official `zksync-os-server` releases. Nothing here is product code.

What the workflow (`.github/workflows/version-experiment.yml`) does, per matrix entry, on a
fresh GitHub-hosted Ubuntu runner:

1. Installs Foundry (`anvil`) with the official `foundry-rs/foundry-toolchain` action.
2. Downloads that release's server binary and `local-chains.tar.gz` from
   `matter-labs/zksync-os-server` and verifies both against the SHA-256 digests GitHub
   publishes for the assets. No third-party server action is used.
3. Starts Anvil (L1) and the server (L2) with the same commands as the repository's
   `run_local.sh`, polls until the RPC answers, and records the startup time.
4. Installs this repository's ZKDoctor and runs `zkdoctor scan` against `127.0.0.1:3050`.
   Endpoints are localhost-only inside the runner.
5. Uploads the scan JSON and diagnostic logs; a final job downloads both scans and runs
   `zkdoctor compare A B`.

| | A | B |
|---|---|---|
| server release | `v0.20.12` | `v0.23.0` |
| local-chain protocol folder | `v31.0` | `v31.0` |
| chain config | `local-chains/v31.0/default/config.yaml` (identical in both tags) | same |

**Confounds to keep in mind.** Each release ships its own `local-chains` bundle (genesis and
L1 state), so differences can come from the server binary, from those bundled files, from a
different ZKsync OS version embedded in the binary, or from run-to-run state. A difference
is not automatically a compatibility break. `summarize.py` prints raw facts only; the
classification is done by reading them.

The workflow runs on `workflow_dispatch` and on pushes that touch it.

## Result (2026-09-21, run 35568950248)

Both releases started (L2 RPC ready in 1.02 s each) and were scanned by the unmodified
V0.1. The comparison reported exactly one difference, the client version
(`zksync-os/v0.20.12` -> `zksync-os/v0.23.0`, WARNING); chain ID, genesis root, execution
version, capabilities and all 11 response shapes were identical. That is a neutral result:
either the versions behave the same on the probed surface, or the probes are too shallow.
See `docs/validation.md`, section 5.

The run also exposed two V0.1 defects (`zkdoctor --version`, and a too-strict genesis
check), fixed in V0.1.1. Re-running this workflow on V0.1.1 is the check that the genesis
scan of the two local servers is now a WARN instead of a FAIL.

Note: check-run annotations are limited to 4,096 characters, so the full scan JSON is only
available from the run's artifacts.
