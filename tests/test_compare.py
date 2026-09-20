import pytest
from conftest import FakeChain, era_methods, load, os_methods

from zkdoctor.client import RpcClient
from zkdoctor.compare import compare_scans
from zkdoctor.evidence import ScanFileError, load_scan, save_scan
from zkdoctor.models import Change, Impact
from zkdoctor.probes import run_scan


def scan_of(methods):
    return run_scan(RpcClient("http://node.test", transport=FakeChain(methods).transport()))


def diffs_of(baseline_methods, target_methods, **kw):
    return compare_scans(scan_of(baseline_methods), scan_of(target_methods), **kw)


def find(cmp, subject, path=None):
    return [d for d in cmp.differences if d.subject == subject and (path is None or d.path == path)]


def test_identical_scans_have_no_differences():
    cmp = diffs_of(os_methods(), os_methods())
    assert cmp.differences == []
    assert len(cmp.unchanged_probes) == 11
    assert cmp.summary["breaking"] == 0


def test_dynamic_block_height_is_not_a_compatibility_change():
    cmp = diffs_of(os_methods(block=1000), os_methods(block=987_654_321))
    assert cmp.differences == []


def test_dynamic_values_in_era_responses_are_ignored():
    cmp = diffs_of(era_methods(block=100, batch=3), era_methods(block=9000, batch=250))
    assert cmp.differences == []


def test_non_critical_capability_loss_is_warning():
    baseline = era_methods()
    target = era_methods()
    del target["zks_L1BatchNumber"]
    cmp = diffs_of(baseline, target)
    [d] = find(cmp, "ZKS-004", "zks_L1BatchNumber")
    assert d.change == Change.REMOVED
    assert d.impact == Impact.WARNING
    assert (d.baseline, d.target) == (True, False)


def test_critical_capability_loss_is_breaking():
    target = era_methods()
    del target["zks_L1BatchNumber"]
    cmp = diffs_of(era_methods(), target, critical=frozenset({"zks_L1BatchNumber"}))
    assert find(cmp, "ZKS-004")[0].impact == Impact.BREAKING


def test_basic_rpc_method_loss_is_breaking_by_default():
    target = os_methods()
    del target["eth_blockNumber"]
    cmp = diffs_of(os_methods(), target)
    assert find(cmp, "RPC-002", "eth_blockNumber")[0].impact == Impact.BREAKING


def test_capability_appearing_is_not_breaking():
    baseline = os_methods()
    target = {**os_methods(), **{k: v for k, v in era_methods().items() if k.startswith("zks_")}}
    cmp = diffs_of(baseline, target)
    added = [d for d in cmp.differences if d.change == Change.ADDED and d.path == "zks_L1BatchNumber"]
    assert added and added[0].impact == Impact.NONE
    assert cmp.summary["breaking"] == 0


def test_field_removed_from_genesis_is_breaking_using_altered_fixture():
    target = os_methods()
    target["zks_getGenesis"] = load("genesis_altered.json")
    cmp = diffs_of(os_methods(), target)
    [d] = find(cmp, "ZKS-001", "execution_version")
    assert d.change == Change.REMOVED and d.impact == Impact.BREAKING
    assert cmp.summary["breaking"] == 1
    # the probe-status change is not double-reported on top of the field diff
    assert len(find(cmp, "ZKS-001")) == 1


def test_field_type_change_is_breaking():
    target = os_methods()
    target["zks_getGenesis"] = {**load("genesis.json"), "execution_version": "0x5"}  # int -> string, still a valid uint
    cmp = diffs_of(os_methods(), target)
    [d] = find(cmp, "ZKS-001", "execution_version")
    assert d.change == Change.CHANGED and d.impact == Impact.BREAKING
    assert (d.baseline, d.target) == ("integer", "string")


def test_new_unknown_field_is_added_not_breaking():
    target = os_methods()
    target["zks_getBlockMetadataByNumber"] = {**load("block_metadata.json"), "gas_limit": "0x1"}
    cmp = diffs_of(os_methods(), target)
    [d] = find(cmp, "ZKS-002", "gas_limit")
    assert d.change == Change.ADDED and d.impact == Impact.NONE
    assert cmp.summary["breaking"] == 0


def test_null_to_value_flip_is_not_a_type_change():
    baseline, target = era_methods(), era_methods()
    good = baseline["zks_getL1BatchDetails"]
    target["zks_getL1BatchDetails"] = lambda p: {**good(p), "commitTxHash": "0x" + "44" * 32}
    assert diffs_of(baseline, target).differences == []


def test_metadata_only_changes_are_warnings():
    target = os_methods(client="zksync-os/0.5.0")
    new_genesis = {**load("genesis.json"), "execution_version": 6, "genesis_root": "0x" + "9" * 64}
    target["zks_getGenesis"] = new_genesis
    target["zks_getBlockMetadataByNumber"] = {**load("block_metadata.json"), "execution_version": 6}
    cmp = diffs_of(os_methods(), target)
    assert cmp.summary["breaking"] == 0
    paths = {d.path: d.impact for d in cmp.differences}
    assert paths["client_version"] == Impact.WARNING
    assert paths["execution_version"] == Impact.WARNING
    assert paths["protocol_metadata.genesis_root"] == Impact.WARNING


def test_chain_id_change_is_breaking():
    cmp = diffs_of(os_methods(chain_id=270), os_methods(chain_id=271))
    d = find(cmp, "environment", "chain_id")[0]
    assert d.impact == Impact.BREAKING and (d.baseline, d.target) == (270, 271)


def test_probe_becoming_fail_is_breaking():
    baseline = os_methods()
    target = os_methods()
    target["eth_chainId"] = "not-hex"
    cmp = diffs_of(baseline, target)
    [d] = find(cmp, "RPC-001")
    assert d.impact == Impact.BREAKING and "PASS -> FAIL" in d.message


def test_fail_to_pass_is_not_breaking():
    baseline = os_methods()
    baseline["eth_chainId"] = "not-hex"
    cmp = diffs_of(baseline, os_methods())
    assert cmp.summary["breaking"] == 0


# ------------------------------------------------------------ persistence


def test_scan_roundtrips_through_file(tmp_path):
    scan = scan_of(os_methods())
    path = tmp_path / "s.json"
    save_scan(scan, path)
    loaded = load_scan(path)
    assert loaded == scan
    assert compare_scans(scan, loaded).differences == []


def test_load_scan_rejects_bad_files(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ScanFileError):
        load_scan(bad)
    bad.write_text('{"schema_version": "9.0"}')
    with pytest.raises(ScanFileError, match="unsupported schema_version"):
        load_scan(bad)
    bad.write_text('{"schema_version": "0.1"}')
    with pytest.raises(ScanFileError, match="invalid scan file"):
        load_scan(bad)
    with pytest.raises(ScanFileError):
        load_scan(tmp_path / "missing.json")
