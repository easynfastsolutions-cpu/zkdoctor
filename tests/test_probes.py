import copy
import json

import httpx
from conftest import FakeChain, RpcFail, era_methods, generic_methods, load, os_methods

from zkdoctor.client import RpcClient
from zkdoctor.evidence import save_scan
from zkdoctor.models import Status
from zkdoctor.probes import check_object, run_scan


def scan_of(methods, url="http://node.test", require=frozenset()):
    chain = FakeChain(methods)
    return run_scan(RpcClient(url, transport=chain.transport()), require)


def by_id(scan):
    return {r.probe_id: r for r in scan.results}


# ------------------------------------------------------------- statuses


def test_zksync_os_scan_pass_skip_mix():
    r = by_id(scan_of(os_methods()))
    for pid in ("RPC-001", "RPC-002", "RPC-003", "RPC-004", "ZKS-001", "ZKS-002", "ZKS-007"):
        assert r[pid].status == Status.PASS, (pid, r[pid].details)
    # L1 batch methods are not exposed: SKIP, never FAIL
    for pid in ("ZKS-003", "ZKS-004", "ZKS-005", "ZKS-006"):
        assert r[pid].status == Status.SKIP, pid


def test_generic_evm_skips_all_zks_probes():
    scan = scan_of(generic_methods())
    assert scan.summary.fail == 0 and scan.summary.error == 0
    assert scan.summary.skip == 7 and scan.summary.passed == 4
    assert scan.environment.detected_execution_environment == "generic-evm"


def test_eravm_scan_passes_batch_probes_with_dynamic_params():
    r = by_id(scan_of(era_methods(block=500, batch=12)))
    for pid in ("ZKS-003", "ZKS-004", "ZKS-005", "ZKS-006"):
        assert r[pid].status == Status.PASS, (pid, r[pid].details)
    assert r["ZKS-005"].evidence.depends_on == ["ZKS-004"]
    assert r["ZKS-005"].evidence.params == [12]


def test_os_documented_method_missing_on_os_node_is_warn_not_fail():
    methods = generic_methods(client="zksync-os/1.0")
    r = by_id(scan_of(methods))
    assert r["ZKS-001"].status == Status.WARN
    assert r["ZKS-002"].status == Status.WARN


def test_require_turns_missing_method_into_fail():
    r = by_id(scan_of(generic_methods(), require=frozenset({"zks_getGenesis"})))
    assert r["ZKS-001"].status == Status.FAIL
    assert r["ZKS-002"].status == Status.SKIP


def test_missing_basic_method_is_fail():
    methods = generic_methods()
    del methods["eth_chainId"]
    assert by_id(scan_of(methods))["RPC-001"].status == Status.FAIL


def test_rpc_error_on_supported_method_is_warn_for_optional_probe():
    methods = os_methods()
    methods["zks_getGenesis"] = RpcFail(-32000, "genesis unavailable")
    r = by_id(scan_of(methods))
    assert r["ZKS-001"].status == Status.WARN
    assert r["ZKS-001"].evidence.rpc_error["message"] == "genesis unavailable"


def test_null_result_for_nullable_probe_is_warn():
    methods = era_methods()
    methods["zks_getBlockDetails"] = lambda p: None
    assert by_id(scan_of(methods))["ZKS-003"].status == Status.WARN


def test_transport_failure_mid_scan_is_error_not_fail():
    chain = FakeChain(os_methods())

    def handler(request):
        if json.loads(request.content)["method"] == "zks_getGenesis":
            raise httpx.ReadTimeout("slow", request=request)
        status, body = chain.respond(json.loads(request.content))
        return httpx.Response(status, json=body)

    scan = run_scan(RpcClient("http://node.test", transport=httpx.MockTransport(handler)))
    result = by_id(scan)["ZKS-001"]
    assert result.status == Status.ERROR
    assert result.evidence.error and result.evidence.response_hash is None
    assert scan.summary.error == 1


# ---------------------------------------------------------------- schema


def test_valid_genesis_passes():
    assert not check_object(load("genesis.json"), {"initial_contracts": "array", "execution_version": "uint"}).problems


def test_missing_required_field_fails():
    methods = os_methods()
    genesis = copy.deepcopy(methods["zks_getGenesis"])
    del genesis["genesis_root"]
    methods["zks_getGenesis"] = genesis
    result = by_id(scan_of(methods))["ZKS-001"]
    assert result.status == Status.FAIL
    assert any("genesis_root" in d for d in result.details)


def test_missing_documented_but_not_required_field_is_warn_not_fail():
    methods = os_methods()
    methods["zks_getGenesis"] = {k: v for k, v in load("genesis.json").items() if k != "execution_version"}
    result = by_id(scan_of(methods))["ZKS-001"]
    assert result.status == Status.WARN
    assert result.details == ["documented field 'execution_version' is absent"]


def test_wrong_type_fails():
    methods = os_methods()
    methods["zks_getGenesis"] = {**load("genesis.json"), "genesis_root": 12345}
    result = by_id(scan_of(methods))["ZKS-001"]
    assert result.status == Status.FAIL
    assert any("genesis_root" in d for d in result.details)


def test_unknown_extra_fields_are_tolerated():
    methods = os_methods()
    methods["zks_getGenesis"] = {**load("genesis.json"), "brand_new_field": {"x": 1}}
    methods["zks_getBlockMetadataByNumber"] = {**load("block_metadata.json"), "future": [1]}
    r = by_id(scan_of(methods))
    assert r["ZKS-001"].status == Status.PASS
    assert r["ZKS-002"].status == Status.PASS


def test_optional_documented_field_wrong_type_is_warn():
    methods = era_methods()
    good = methods["zks_getBlockDetails"]
    methods["zks_getBlockDetails"] = lambda p: {**good(p), "status": 5}
    assert by_id(scan_of(methods))["ZKS-003"].status == Status.WARN


# -------------------------------------------------------------- evidence


def test_evidence_is_hashed_bounded_and_utc():
    methods = os_methods()
    methods["zks_getGenesis"] = {
        **load("genesis.json"),
        "initial_contracts": [["0x" + "00" * 20, "0x" + "ab" * 50_000]] * 3,
    }
    scan = scan_of(methods)
    ev = by_id(scan)["ZKS-001"].evidence
    assert len(ev.request_hash) == 64 and len(ev.response_hash) == 64
    assert ev.timestamp.endswith("Z")
    assert ev.result_summary["type"] == "object"
    assert ev.result_summary["shape"]["execution_version"] == "integer"
    assert ev.result_summary["size_bytes"] > 100_000
    # the bytecode itself is not persisted
    assert len(json.dumps(ev.model_dump(mode="json"))) < 5_000


def test_same_request_same_hash_across_scans():
    a = by_id(scan_of(os_methods(block=10)))["ZKS-001"].evidence
    b = by_id(scan_of(os_methods(block=99999)))["ZKS-001"].evidence
    assert a.request_hash == b.request_hash and a.response_hash == b.response_hash
    c = by_id(scan_of(os_methods(block=10)))["ZKS-002"].evidence
    d = by_id(scan_of(os_methods(block=99999)))["ZKS-002"].evidence
    assert c.request_hash != d.request_hash  # dynamic param differs


def test_no_numeric_scores_in_output(tmp_path):
    path = tmp_path / "scan.json"
    save_scan(scan_of(os_methods()), path)
    text = path.read_text().lower()
    assert "score" not in text and "%" not in text


def test_scan_json_top_level_structure(tmp_path):
    path = tmp_path / "scan.json"
    save_scan(scan_of(os_methods()), path)
    data = json.loads(path.read_text())
    assert list(data) == [
        "schema_version", "tool_version", "scan_id", "timestamp",
        "environment", "capabilities", "results", "summary",
    ]
    assert set(data["summary"]) == {"pass", "warn", "fail", "skip", "error"}
    assert [r["probe_id"] for r in data["results"]][:2] == ["RPC-001", "RPC-002"]


# ------------------------------------------------------------- redaction


def test_credentials_never_reach_scan_file(tmp_path):
    url = "https://alice:hunter2@node.test:8545/v2/abcdefghijklmnopqrstuvwxyz123456?apikey=SECRETVAL"
    path = tmp_path / "scan.json"
    save_scan(scan_of(os_methods(), url=url), path)
    text = path.read_text()
    for secret in ("alice", "hunter2", "abcdefghijklmnopqrstuvwxyz123456", "SECRETVAL"):
        assert secret not in text
    assert "node.test:8545" in text


# ------------------------------------- regression: real testnet (zksync-os/v0.24.0)


def test_real_testnet_shape_has_no_false_failures():
    from conftest import real_testnet_methods

    scan = scan_of(real_testnet_methods())
    r = by_id(scan)
    assert scan.summary.fail == 0 and scan.summary.error == 0
    assert scan.environment.detected_execution_environment == "zksync-os"
    assert scan.environment.execution_version is None  # the node does not report one
    # docs/node disagreements are surfaced as WARN, not hidden and not FAIL
    assert r["ZKS-001"].status == Status.WARN
    assert "documented field 'execution_version' is absent" in r["ZKS-001"].details
    assert r["ZKS-002"].status == Status.WARN
    assert r["RPC-004"].status == Status.WARN  # hex net_version, docs say decimal
    assert r["ZKS-007"].status == Status.PASS
    for pid in ("ZKS-003", "ZKS-004", "ZKS-005", "ZKS-006"):
        assert r[pid].status == Status.SKIP


def test_block_metadata_is_queried_with_an_integer_not_hex():
    from conftest import real_testnet_methods

    chain = FakeChain(real_testnet_methods(block=504553))
    run_scan(RpcClient("http://node.test", transport=chain.transport()))
    assert ("zks_getBlockMetadataByNumber", [504553]) in chain.calls
