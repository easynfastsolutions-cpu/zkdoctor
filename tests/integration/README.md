# Optional integration tests

These run ZKDoctor against a **real** ZKsync OS node. They are skipped unless
`ZKDOCTOR_INTEGRATION_RPC` is set, and nothing in the normal `pytest` run needs Rust,
Foundry or Docker.

## Workflow (local ZKsync OS server)

The official server repository documents a local run with an in-memory-style L1
(Anvil) and the JSON-RPC endpoint on port **3050**
([`docs/src/setup/local_run.md`](https://github.com/matter-labs/zksync-os-server/blob/main/docs/src/setup/local_run.md)).
Prerequisites there: Rust toolchain and Foundry (`anvil`).

```bash
# in a checkout of https://github.com/matter-labs/zksync-os-server
gzip -dfk ./local-chains/v30.2/l1-state.json.gz
./run_local.sh ./local-chains/v30.2/default
```

Then, in this repo:

```bash
ZKDOCTOR_INTEGRATION_RPC=http://localhost:3050 uv run pytest tests/integration -v
```

To try the whole tool against it:

```bash
uv run zkdoctor scan --rpc http://localhost:3050 --output baseline.json
# change version/config, restart the node, then:
uv run zkdoctor scan --rpc http://localhost:3050 --output target.json
uv run zkdoctor compare baseline.json target.json
```

## The official integration-test suite

[`matter-labs/zksync-os-integration-tests`](https://github.com/matter-labs/zksync-os-integration-tests)
(`cargo test -p tests --release`) starts Anvil, deploys contracts and runs in-process
ZKsync OS servers for the duration of a test. Its servers live only inside the Rust test
process, so ZKDOCTOR cannot point at them from outside without modifying that suite
(for example, a test that starts a chain and then waits). That is deliberately **not**
done in V0. Use `run_local.sh` above for a persistent endpoint.

## Status

- **Public testnet (Route A): executed 2026-09-20.** The 3 tests pass against
  `https://zksync-os-testnet-alpha.zksync.dev/` (`zksync-os/v0.24.0`); findings are in
  `docs/sources.md`.
- **Local `run_local.sh` node (Route B): not executed.** It needs Rust, Foundry and
  several GB of disk that were not available in the development environment. Route B is
  still the preferred next step, because it lets you pin and deliberately alter the
  server version (e.g. across v0.4.0 / v0.5.0) and exercise `compare` for real.
