"""Optional live checks. Skipped unless ZKDOCTOR_INTEGRATION_RPC points at a running node.

See tests/integration/README.md. Nothing here is required for the normal test run.
"""

import os

import pytest

from zkdoctor.client import RpcClient
from zkdoctor.compare import compare_scans
from zkdoctor.models import Status
from zkdoctor.probes import run_scan

RPC = os.environ.get("ZKDOCTOR_INTEGRATION_RPC")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not RPC, reason="set ZKDOCTOR_INTEGRATION_RPC to run"),
]


@pytest.fixture(scope="module")
def scans():
    client = RpcClient(RPC, timeout=15)
    try:
        return run_scan(client), run_scan(client)
    finally:
        client.close()


def test_live_scan_has_no_transport_errors_or_failures(scans):
    first, _ = scans
    assert first.summary.error == 0, [r for r in first.results if r.status == Status.ERROR]
    assert first.summary.fail == 0, [(r.probe_id, r.details) for r in first.results if r.status == Status.FAIL]


def test_live_generic_probes_pass(scans):
    first, _ = scans
    generic = {r.probe_id: r.status for r in first.results if r.category == "generic"}
    assert generic["RPC-001"] == Status.PASS and generic["RPC-002"] == Status.PASS


def test_two_back_to_back_scans_show_no_breaking_difference(scans):
    """Block height advances between scans; that must not register as a compatibility change."""
    first, second = scans
    assert compare_scans(first, second).summary["breaking"] == 0
