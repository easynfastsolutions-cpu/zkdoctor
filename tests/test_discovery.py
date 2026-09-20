import httpx
import pytest
from conftest import FakeChain, era_methods, generic_methods, os_methods

from zkdoctor.client import RpcClient
from zkdoctor.discovery import CallPlan, DiscoveryAborted, discover, is_method_missing
from zkdoctor.probes import PROBES


def plans():
    return [CallPlan(s.id, s.method, s.depends_on, s.params, s.provides) for s in PROBES]


def run_discovery(methods):
    chain = FakeChain(methods)
    return chain, discover(RpcClient("http://node.test", transport=chain.transport()), plans())


def test_standard_evm_chain_has_no_zks_capabilities():
    _, found = run_discovery(generic_methods(chain_id=1, client="Geth/v1.13"))
    env = found.environment
    assert env.chain_id == 1
    assert env.detected_execution_environment == "generic-evm"
    assert found.capabilities["eth_chainId"].supported is True
    assert found.capabilities["zks_getGenesis"].supported is False
    assert found.capabilities["zks_getBlockMetadataByNumber"].supported is False


def test_zksync_os_environment_detected_and_identity_extracted():
    chain, found = run_discovery(os_methods(block=1234, chain_id=270))
    env = found.environment
    assert env.detected_execution_environment == "zksync-os"
    assert env.chain_id == 270
    assert env.execution_version == 5
    assert env.protocol_metadata["genesis_root"].startswith("0x1111")
    assert env.client_version == "zksync-os/0.4.0"
    assert env.endpoint == "http://node.test"
    assert len(env.endpoint_fingerprint) == 16
    # block metadata was queried with the block number discovered earlier, not a fixed one
    assert ("zks_getBlockMetadataByNumber", [1234]) in chain.calls  # integer, as the real server requires


def test_missing_zksync_methods_are_unsupported_not_errors():
    _, found = run_discovery(os_methods())
    assert found.capabilities["zks_L1BatchNumber"].supported is False
    # dependents of an unsupported method cannot be determined
    assert found.capabilities["zks_getL1BatchDetails"].supported is None
    assert found.observed["ZKS-006"].skipped_reason


def test_eravm_environment_uses_dynamic_batch_number():
    chain, found = run_discovery(era_methods(block=500, batch=77))
    assert found.environment.detected_execution_environment == "zksync-eravm"
    assert ("zks_getL1BatchDetails", [77]) in chain.calls
    assert ("zks_getBlockDetails", [500]) in chain.calls
    assert found.environment.protocol_metadata["protocol_version"] == "Version27"


def test_only_bridgehub_is_reported_as_unknown_zksync():
    methods = {**generic_methods(), "zks_getBridgehubContract": "0x" + "ab" * 20}
    _, found = run_discovery(methods)
    assert found.environment.detected_execution_environment == "zksync-unknown"


def test_client_version_alone_identifies_zksync_os_when_methods_missing():
    _, found = run_discovery(generic_methods(client="zksync-os/0.9"))
    assert found.environment.detected_execution_environment == "zksync-os"


def test_unreachable_endpoint_aborts_discovery():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(DiscoveryAborted):
        discover(RpcClient("http://node.test", transport=httpx.MockTransport(handler)), plans())


def test_is_method_missing_variants():
    assert is_method_missing({"code": -32601, "message": "x"})
    assert is_method_missing({"code": -32000, "message": "the method zks_x does not exist/is not available"})
    assert not is_method_missing({"code": -32602, "message": "invalid params"})
    assert not is_method_missing(None)
