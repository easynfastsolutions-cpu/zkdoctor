"""Tests for the experimental `watch` command.

Nodes are simulated in-process (httpx.MockTransport) and time is a fake clock advanced by the
`sleep` hook, so a five-minute stall runs instantly and deterministically.
"""

import hashlib
import json
import re

import httpx
import pytest
from conftest import FakeChain, generic_methods
from typer.testing import CliRunner

from zkdoctor.cli import app
from zkdoctor.client import RpcClient
from zkdoctor.evidence import build_evidence
from zkdoctor.models import RpcObservation
from zkdoctor.watch import (
    HealthResult,
    Thresholds,
    WatchConfigError,
    format_line,
    make_health_probe,
    parse_duration,
    replay,
    run_watch,
)

runner = CliRunner()


class SimNode:
    """A scripted endpoint. The test sets height/failures; block hashes are a function of (salt, number)."""

    def __init__(self, chain_id=506, height=1000, salt="main"):
        self.chain_id, self.height, self.salt = chain_id, height, salt
        self.down = False
        self.hash_unsupported = False
        self.error_methods = set()
        self.calls = []

    def block_hash(self, n):
        return "0x" + hashlib.sha256(f"{self.salt}:{n}".encode()).hexdigest()

    def client(self, url="http://node.test"):
        def handler(request):
            if self.down:
                raise httpx.ReadTimeout("timed out", request=request)
            body = json.loads(request.content)
            method, params = body["method"], body["params"]
            self.calls.append((method, params))
            reply = {"jsonrpc": "2.0", "id": body["id"]}
            if method in self.error_methods:
                reply["error"] = {"code": -32000, "message": "backend unavailable"}
            elif method == "eth_chainId":
                reply["result"] = hex(self.chain_id)
            elif method == "eth_blockNumber":
                reply["result"] = hex(self.height)
            elif method == "eth_getBlockByNumber" and not self.hash_unsupported:
                n = int(params[0], 16)
                reply["result"] = {"number": params[0], "hash": self.block_hash(n)} if n <= self.height else None
            else:
                reply["error"] = {"code": -32601, "message": "Method not found"}
            return httpx.Response(200, json=reply)

        return RpcClient(url, transport=httpx.MockTransport(handler))


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def run_sim(target, reference, *, polls, script=None, interval=30.0, thresholds=None, health=None,
            output=None, target_url="http://node.test", reference_url="http://reference.test"):
    clock, step, lines = Clock(), {"n": 0}, []

    def sleep(seconds):
        clock.t += seconds
        step["n"] += 1
        if script:
            script(step["n"], target, reference)

    result = run_watch(
        target.client(target_url), reference.client(reference_url), thresholds or Thresholds(), interval,
        output=output, health=health, max_polls=polls, clock=clock, sleep=sleep,
        emit=lambda status, line: lines.append((status, line)),
    )
    return result, lines


def read(path):
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return (
        [r for r in records if r["kind"] == "meta"],
        [r for r in records if r["kind"] == "poll"],
        [r for r in records if r["kind"] == "end"],
    )


def codes(poll):
    return [f["code"] for f in poll["findings"]]


def frozen_target_script(step, target, reference):
    reference.height += 4  # reference keeps producing blocks; the target never moves


def healthy():
    obs = RpcObservation(method="GET http://status.test/", params=[], timestamp="2026-01-01T00:00:00.000Z",
                         http_status=200, result={"healthy": True}, latency_ms=1.0)
    return HealthResult("healthy", build_evidence("WATCH-HEALTH", "GET http://status.test/", [], obs, []))


# ------------------------------------------------------------------------- lag


def test_both_advancing_with_normal_lag_is_ok_and_exits_zero(tmp_path):
    target, reference = SimNode(height=999), SimNode(height=1000)

    def script(step, t, r):
        t.height += 2
        r.height += 2

    result, lines = run_sim(target, reference, polls=10, script=script, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    assert result.exit_code == 0 and result.reason == "max_polls"
    assert all(p["status"] == "PASS" and p["findings"] == [] for p in polls)
    assert all(p["blocks_behind"] == 1 and p["target"]["changed"] in (None, True) for p in polls)
    assert lines[1][1].endswith("lag=1 stale=0s")


def test_target_one_block_behind_is_not_a_finding(tmp_path):
    run_sim(SimNode(height=1000), SimNode(height=1001), polls=4, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    assert {p["blocks_behind"] for p in polls} == {1} and {p["status"] for p in polls[:1]} == {"PASS"}


def test_target_increasingly_behind_warns_but_does_not_fail(tmp_path):
    def script(step, t, r):
        t.height += 1
        r.height += 3  # the target advances, only slower

    result, _ = run_sim(SimNode(), SimNode(), polls=8, script=script, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    assert [p["blocks_behind"] for p in polls] == [0, 2, 4, 6, 8, 10, 12, 14]
    assert polls[1]["status"] == "PASS"  # exactly at the threshold: warn is "> 2"
    assert all("TARGET_LAGGING" in codes(p) and p["status"] == "WARN" for p in polls[2:])
    assert not any("TARGET_STALLED" in codes(p) for p in polls)  # it is moving, so not a stall
    assert result.exit_code == 0


def test_frozen_target_with_advancing_reference_escalates_warn_then_fail(tmp_path):
    result, _ = run_sim(SimNode(), SimNode(), polls=40, script=frozen_target_script, output=tmp_path / "w.jsonl")
    _, polls, end = read(tmp_path / "w.jsonl")
    by_seq = {p["seq"]: p for p in polls}
    assert "TARGET_LAGGING" in codes(by_seq[2]) and "TARGET_STALLED" not in codes(by_seq[2])
    assert "TARGET_STALLED" not in codes(by_seq[3])  # stale=60s is not > 60s
    assert by_seq[4]["findings"][0]["code"] == "TARGET_STALLED" and by_seq[4]["findings"][0]["status"] == "WARN"
    assert by_seq[10]["status"] == "WARN"  # stale=270s
    assert by_seq[11]["status"] == "FAIL" and by_seq[11]["target"]["seconds_since_change"] == 300  # >= 300s
    assert by_seq[11]["fail_streak"] == 1
    assert by_seq[12]["fail_streak"] == 2
    assert result.exit_code == 1 and result.reason == "sustained_fail" and result.polls == 12
    assert end[-1]["reason"] == "sustained_fail" and end[-1]["exit_code"] == 1


def test_fail_after_is_configurable(tmp_path):
    result, _ = run_sim(SimNode(), SimNode(), polls=40, script=frozen_target_script,
                        thresholds=Thresholds(fail_after=1), output=tmp_path / "w.jsonl")
    assert result.exit_code == 1 and result.polls == 11
    result, _ = run_sim(SimNode(), SimNode(), polls=40, script=frozen_target_script,
                        thresholds=Thresholds(fail_after=3))
    assert result.exit_code == 1 and result.polls == 13


def test_reference_also_frozen_is_both_stalled_never_a_target_failure(tmp_path):
    result, _ = run_sim(SimNode(), SimNode(), polls=20, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    assert polls[0]["status"] == "PASS" and polls[2]["status"] == "PASS"  # stale=0s, 60s (not > 60s)
    stalled = [p for p in polls if "BOTH_STALLED" in codes(p)]
    assert stalled[0]["seq"] == 4  # stale=90s
    assert len(stalled) == 17 and all(p["status"] == "WARN" for p in stalled)  # WARN at any duration
    assert not any(c in codes(p) for p in polls for c in ("TARGET_STALLED", "HEALTH_CHECK_FALSE_POSITIVE"))
    assert result.exit_code == 0 and result.reason == "max_polls"


def test_stalled_reference_while_target_advances_is_reference_stalled(tmp_path):
    def script(step, t, r):
        t.height += 1  # only the target moves

    result, _ = run_sim(SimNode(), SimNode(), polls=8, script=script, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    flagged = [p for p in polls if "REFERENCE_STALLED" in codes(p)]
    assert flagged and all(p["status"] == "WARN" for p in flagged)
    assert result.exit_code == 0


def test_transient_rpc_failure_is_an_error_poll_not_a_stall(tmp_path):
    def script(step, t, r):
        t.height += 1
        r.height += 1
        t.down = step == 3  # the request before poll 4 times out, once

    result, _ = run_sim(SimNode(), SimNode(), polls=8, script=script, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    assert polls[3]["status"] == "ERROR" and codes(polls[3]) == ["RPC_ERROR"]
    assert "not treated as a stall" in polls[3]["findings"][0]["message"]
    assert polls[3]["target"]["height"] is None and polls[3]["target"]["error"]
    assert polls[3]["target"]["evidence"]["error"]  # the failed request is still recorded
    assert polls[3]["fail_streak"] == 0
    assert all(p["status"] == "PASS" for i, p in enumerate(polls) if i != 3)
    assert result.exit_code == 0


def test_stale_timer_restarts_after_an_error_so_an_outage_is_never_counted_as_a_stall(tmp_path):
    def script(step, t, r):
        r.height += 4
        t.down = step == 5  # error before poll 6 (t=150s), target otherwise frozen

    run_sim(SimNode(), SimNode(), polls=8, script=script, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    assert "TARGET_STALLED" in codes(polls[4])  # poll 5, stale=120s
    assert polls[5]["status"] == "ERROR"
    assert "TARGET_STALLED" not in codes(polls[6])  # poll 7: timer restarted after the error
    assert polls[6]["target"]["seconds_since_change"] == 0


def test_a_bounded_run_with_no_usable_observation_is_a_tool_error_not_a_pass(tmp_path):
    target = SimNode()
    target.error_methods = {"eth_blockNumber"}  # chain ID answers (preflight passes) but heights never do
    result, _ = run_sim(target, SimNode(), polls=3, output=tmp_path / "w.jsonl")
    _, polls, end = read(tmp_path / "w.jsonl")
    assert [p["status"] for p in polls] == ["ERROR"] * 3
    assert all(codes(p) == ["RPC_ERROR"] and p["fail_streak"] == 0 for p in polls)
    assert result.exit_code == 2 and result.reason == "no_usable_observations"
    assert end[-1]["exit_code"] == 2


def test_errors_alone_never_reach_a_sustained_fail(tmp_path):
    def script(step, t, r):
        t.down = step >= 1  # the target goes away right after the first poll

    result, _ = run_sim(SimNode(), SimNode(), polls=30, script=script, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    assert polls[0]["status"] == "PASS" and {p["status"] for p in polls[1:]} == {"ERROR"}
    assert result.exit_code == 0 and result.polls == 30  # 29 error polls never became a FAIL


# ---------------------------------------------------------------- health mismatch


def test_adi_shaped_health_mismatch_is_emitted_only_after_the_stale_condition(tmp_path):
    """target head frozen, reference advancing, target health = healthy"""
    result, _ = run_sim(SimNode(), SimNode(), polls=40, script=frozen_target_script, health=healthy,
                        output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    first = next(p for p in polls if "HEALTH_CHECK_FALSE_POSITIVE" in codes(p))
    assert first["seq"] == 11 and first["target"]["seconds_since_change"] == 300
    assert not any("HEALTH_CHECK_FALSE_POSITIVE" in codes(p) for p in polls if p["seq"] < 11)
    finding = next(f for f in first["findings"] if f["code"] == "HEALTH_CHECK_FALSE_POSITIVE")
    assert finding["status"] == "FAIL"
    assert "conflicts with observed chain progression" in finding["message"]
    assert first["health"]["status"] == "healthy" and first["health"]["evidence"]["probe_id"] == "WATCH-HEALTH"
    assert result.exit_code == 1 and result.polls == 12


def test_health_mismatch_follows_configured_stale_fail(tmp_path):
    th = Thresholds(stale_warn_s=60, stale_fail_s=120)
    run_sim(SimNode(), SimNode(), polls=40, script=frozen_target_script, health=healthy, thresholds=th,
            output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    seqs = [p["seq"] for p in polls if "HEALTH_CHECK_FALSE_POSITIVE" in codes(p)]
    assert seqs[0] == 5  # 120s
    assert "TARGET_STALLED" in codes(polls[3])  # 90s: already a WARN stall...
    assert "HEALTH_CHECK_FALSE_POSITIVE" not in codes(polls[3])  # ...but not yet the health conflict


def test_health_mismatch_is_not_flagged_when_health_is_unhealthy_unsupported_or_reference_also_frozen(tmp_path):
    def unhealthy():
        h = healthy()
        return HealthResult("unhealthy", h.evidence)

    for health in (unhealthy, None):
        result, _ = run_sim(SimNode(), SimNode(), polls=14, script=frozen_target_script, health=health,
                            output=tmp_path / f"{getattr(health, '__name__', 'none')}.jsonl")
        _, polls, _ = read(tmp_path / f"{getattr(health, '__name__', 'none')}.jsonl")
        assert not any("HEALTH_CHECK_FALSE_POSITIVE" in codes(p) for p in polls)
        assert any("TARGET_STALLED" in codes(p) for p in polls)  # the stall itself is still reported
    run_sim(SimNode(), SimNode(), polls=14, health=healthy, output=tmp_path / "both.jsonl")  # reference frozen too
    _, polls, _ = read(tmp_path / "both.jsonl")
    assert not any("HEALTH_CHECK_FALSE_POSITIVE" in codes(p) for p in polls)


def test_health_check_unsupported_is_recorded_when_none_is_provided(tmp_path):
    run_sim(SimNode(), SimNode(), polls=2, output=tmp_path / "w.jsonl")
    meta, polls, _ = read(tmp_path / "w.jsonl")
    assert meta[0]["health"] == {"configured": False, "url": None}
    assert polls[0]["health"]["status"] == "unsupported"


# ---------------------------------------------------------------- state agreement


def test_same_hash_at_common_height_passes(tmp_path):
    run_sim(SimNode(height=1000), SimNode(height=1002), polls=2, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    state = polls[0]["state_check"]
    assert state["status"] == "PASS" and state["common_height"] == 1000
    assert state["target_hash"] == state["reference_hash"]


def test_different_hash_at_common_height_is_state_divergence_fail(tmp_path):
    target, reference = SimNode(salt="other"), SimNode(salt="main")
    result, _ = run_sim(target, reference, polls=5, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    p = polls[0]
    assert p["status"] == "FAIL" and codes(p) == ["STATE_DIVERGENCE"]
    state = p["state_check"]
    assert state["status"] == "FAIL" and state["common_height"] == 1000
    assert state["target_hash"] == target.block_hash(1000) and state["reference_hash"] == reference.block_hash(1000)
    for side in ("target", "reference"):
        ev = state["evidence"][side]
        assert ev["method"] == "eth_getBlockByNumber" and ev["params"] == [hex(1000), False]
        assert len(ev["request_hash"]) == 64 and len(ev["response_hash"]) == 64
        assert ev["depends_on"] == ["WATCH-TARGET-HEIGHT", "WATCH-REFERENCE-HEIGHT"]
    assert "fork" not in p["findings"][0]["message"].lower()  # divergence, not a claimed fork
    assert result.exit_code == 1 and result.polls == 2  # fail_after=2 consecutive


def test_common_height_is_the_minimum_whichever_node_is_ahead(tmp_path):
    ahead, behind = SimNode(height=1010), SimNode(height=1005)
    run_sim(ahead, behind, polls=1, output=tmp_path / "a.jsonl")
    _, polls, _ = read(tmp_path / "a.jsonl")
    assert polls[0]["state_check"]["common_height"] == 1005 and polls[0]["blocks_behind"] == -5
    assert polls[0]["status"] == "PASS"  # target ahead is not lag
    assert ("eth_getBlockByNumber", [hex(1005), False]) in ahead.calls
    assert ("eth_getBlockByNumber", [hex(1005), False]) in behind.calls
    assert not any(c[0] == "eth_getBlockByNumber" and c[1][0] == hex(1010) for c in ahead.calls + behind.calls)

    run_sim(SimNode(height=1000), SimNode(height=1003), polls=1, output=tmp_path / "b.jsonl")
    _, polls, _ = read(tmp_path / "b.jsonl")
    assert polls[0]["state_check"]["common_height"] == 1000
    assert "TARGET_LAGGING" in codes(polls[0])


def test_different_chain_ids_are_an_immediate_configuration_error(tmp_path):
    target, reference = SimNode(chain_id=506), SimNode(chain_id=36900)
    out = tmp_path / "w.jsonl"
    with pytest.raises(WatchConfigError, match="chain ID mismatch: target 506 vs reference 36900"):
        run_sim(target, reference, polls=3, output=out)
    assert [c[0] for c in target.calls + reference.calls] == ["eth_chainId", "eth_chainId"]  # nothing else was asked
    assert not out.exists()  # no state compared, nothing recorded


def test_unsupported_block_hash_method_is_skip_not_a_failure(tmp_path):
    target = SimNode()
    target.hash_unsupported = True
    result, _ = run_sim(target, SimNode(), polls=3, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    assert all(p["state_check"]["status"] == "SKIP" and p["status"] == "PASS" for p in polls)
    assert "not supported" in polls[0]["state_check"]["reason"]
    assert result.exit_code == 0


# ---------------------------------------------------------------------- evidence


def test_every_poll_preserves_request_response_hash_latency_and_timestamp(tmp_path):
    run_sim(SimNode(), SimNode(), polls=3, output=tmp_path / "w.jsonl")
    _, polls, _ = read(tmp_path / "w.jsonl")
    assert len(polls) == 3
    for p in polls:
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", p["timestamp"])
        for side, probe in (("target", "WATCH-TARGET-HEIGHT"), ("reference", "WATCH-REFERENCE-HEIGHT")):
            ev = p[side]["evidence"]
            assert ev["probe_id"] == probe and ev["method"] == "eth_blockNumber" and ev["params"] == []
            assert len(ev["request_hash"]) == 64 and len(ev["response_hash"]) == 64
            assert isinstance(ev["latency_ms"], float) and ev["timestamp"].endswith("Z")
            assert ev["http_status"] == 200 and ev["result_summary"]["type"] == "string"
            assert p[side]["endpoint"].startswith("http://") and len(p[side]["fingerprint"]) == 16
        assert p["peer_count"]["status"] == "SKIP"  # never a finding on its own


def test_credentials_never_reach_the_jsonl_or_the_terminal(tmp_path):
    secret_url = "https://alice:hunter2@node.test:8545/v2/abcdefghijklmnopqrstuvwxyz123456?apikey=SECRETVAL"
    result, lines = run_sim(SimNode(), SimNode(), polls=2, output=tmp_path / "w.jsonl",
                            target_url=secret_url, reference_url=secret_url)
    everything = (tmp_path / "w.jsonl").read_text() + "\n".join(line for _, line in lines)
    for secret in ("alice", "hunter2", "abcdefghijklmnopqrstuvwxyz123456", "SECRETVAL"):
        assert secret not in everything
    assert "node.test:8545" in everything


# ------------------------------------------------------------------------- JSONL


def test_jsonl_has_a_metadata_record_then_one_record_per_poll(tmp_path):
    out = tmp_path / "w.jsonl"
    run_sim(SimNode(), SimNode(), polls=5, output=out, interval=15.0,
            thresholds=Thresholds(lag_warn_blocks=4, stale_warn_s=90, stale_fail_s=600, fail_after=3))
    kinds = [json.loads(line)["kind"] for line in out.read_text().splitlines()]
    assert kinds == ["meta"] + ["poll"] * 5 + ["end"]
    meta = json.loads(out.read_text().splitlines()[0])
    assert meta["schema"] == "zkdoctor-watch/0.1" and meta["watch_version"] == "0.1-experimental"
    assert meta["target"]["chain_id"] == 506 == meta["reference"]["chain_id"] == meta["chain_id"]
    assert meta["target"]["evidence"]["method"] == "eth_chainId"
    assert meta["thresholds"] == {"lag_warn_blocks": 4, "stale_warn_s": 90, "stale_fail_s": 600, "fail_after": 3}
    assert meta["interval_s"] == 15.0 and meta["started"].endswith("Z")
    assert meta["health"]["configured"] is False
    assert [json.loads(line)["seq"] for line in out.read_text().splitlines()[1:6]] == [1, 2, 3, 4, 5]


def test_jsonl_is_append_only_across_runs(tmp_path):
    out = tmp_path / "w.jsonl"
    run_sim(SimNode(), SimNode(), polls=2, output=out)
    first = out.read_text()
    run_sim(SimNode(), SimNode(), polls=3, output=out)
    second = out.read_text()
    assert second.startswith(first) and len(second) > len(first)
    metas, polls, ends = read(out)
    assert len(metas) == 2 and len(polls) == 5 and len(ends) == 2


def test_jsonl_can_be_replayed_offline_to_the_same_findings(tmp_path):
    out = tmp_path / "w.jsonl"
    run_sim(SimNode(), SimNode(), polls=40, script=frozen_target_script, health=healthy, output=out)
    _, polls, _ = read(out)
    replayed = replay(out)  # no client, no network
    assert [(r["seq"], r["status"], r["codes"]) for r in replayed] == [(p["seq"], p["status"], codes(p)) for p in polls]
    assert "HEALTH_CHECK_FALSE_POSITIVE" in replayed[-1]["codes"]


def test_format_line_matches_the_documented_shape():
    record = {
        "timestamp": "2026-09-21T12:31:00.000Z", "status": "PASS", "findings": [], "blocks_behind": 1,
        "target": {"height": 1253580, "seconds_since_change": 0.0}, "reference": {"height": 1253581},
    }
    assert format_line(record) == "12:31:00  OK    target=1253580 reference=1253581 lag=1 stale=0s"
    record.update(status="FAIL", findings=[{"code": "HEALTH_CHECK_FALSE_POSITIVE"}], blocks_behind=127)
    record["target"]["seconds_since_change"] = 300.0
    assert format_line(record) == (
        "12:31:00  FAIL  HEALTH_CHECK_FALSE_POSITIVE target=1253580 reference=1253581 lag=127 stale=300s"
    )


# ------------------------------------------------------------------ health probe


def health_result(body=None, status=200, text=None, url="http://status.test/status"):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)

    return make_health_probe(url, transport=httpx.MockTransport(handler))(), seen


def test_health_probe_interprets_only_explicit_signals():
    assert health_result({"healthy": True})[0].status == "healthy"
    assert health_result({"healthy": False})[0].status == "unhealthy"
    assert health_result({"status": "ok"})[0].status == "healthy"
    assert health_result({"something": "else"})[0].status == "unknown"  # not assumed healthy
    assert health_result(text="hello")[0].status == "unknown"
    assert health_result({"healthy": True}, status=404)[0].status == "error"  # wrong path is not "healthy"


def test_health_probe_requests_exactly_the_given_url_and_redacts_it():
    result, seen = health_result({"healthy": True}, url="http://alice:hunter2@status.test:3071/status?token=SECRETVAL")
    assert len(seen) == 1 and "/status" in seen[0]
    ev = result.evidence
    assert ev.method.startswith("GET http://status.test:3071/status")
    assert "hunter2" not in ev.model_dump_json() and "SECRETVAL" not in ev.model_dump_json()
    assert len(ev.request_hash) == 64 and len(ev.response_hash) == 64 and ev.latency_ms is not None


def test_health_probe_rejects_a_non_url():
    with pytest.raises(WatchConfigError):
        make_health_probe("not a url")


# ------------------------------------------------------------ options and CLI


def test_parse_duration_and_threshold_validation():
    assert parse_duration("30s") == 30 and parse_duration("5m") == 300 and parse_duration("2h") == 7200
    assert parse_duration("500ms") == 0.5 and parse_duration("45") == 45
    with pytest.raises(ValueError):
        parse_duration("soon")
    with pytest.raises(WatchConfigError):
        Thresholds(stale_warn_s=300, stale_fail_s=60).validate()
    with pytest.raises(WatchConfigError):
        Thresholds(fail_after=0).validate()
    with pytest.raises(WatchConfigError):
        Thresholds(lag_warn_blocks=-1).validate()


def cli(*args):
    return runner.invoke(app, list(args))


def test_cli_healthy_watch_exits_zero_and_writes_jsonl(tmp_path):
    out = tmp_path / "w.jsonl"
    with FakeChain(generic_methods(block=500, chain_id=506)).serve() as a, \
            FakeChain(generic_methods(block=500, chain_id=506)).serve() as b:
        result = cli("watch", "--rpc", a, "--reference", b, "--interval", "50ms", "--max-polls", "3", "--output", str(out))
    assert result.exit_code == 0, result.output
    assert result.output.count("  OK    ") == 3 and "health=unsupported" in result.output
    assert "watch ended: max_polls after 3 polls (exit 0)" in result.output
    assert len(read(out)[1]) == 3


def test_cli_sustained_fail_exits_non_zero():
    counter = {"n": 1000}

    def advancing(_params):
        counter["n"] += 1
        return hex(counter["n"])

    reference_methods = {**generic_methods(chain_id=506), "eth_blockNumber": advancing}
    with FakeChain(generic_methods(block=1000, chain_id=506)).serve() as target, \
            FakeChain(reference_methods).serve() as reference:
        result = cli("watch", "--rpc", target, "--reference", reference, "--interval", "100ms", "--stale-warn", "150ms",
                     "--stale-fail", "250ms", "--lag-warn-blocks", "100000", "--max-polls", "40")
    assert result.exit_code == 1, result.output
    assert "TARGET_STALLED" in result.output and "sustained_fail" in result.output


def test_cli_chain_mismatch_and_bad_configuration_are_tool_errors(tmp_path):
    with FakeChain(generic_methods(chain_id=506)).serve() as a, FakeChain(generic_methods(chain_id=507)).serve() as b:
        mismatch = cli("watch", "--rpc", a, "--reference", b, "--max-polls", "1")
        ok = cli("watch", "--rpc", a, "--reference", a, "--stale-warn", "10m", "--stale-fail", "5m")
        bad_interval = cli("watch", "--rpc", a, "--reference", a, "--interval", "soon")
    assert mismatch.exit_code == 2 and "chain ID mismatch" in mismatch.output
    assert ok.exit_code == 2 and "--stale-warn" in ok.output
    assert bad_interval.exit_code == 2 and "invalid duration" in bad_interval.output
    assert cli("watch", "--rpc", "not-a-url", "--reference", "http://x.test").exit_code == 2
    assert cli("watch", "--rpc", "http://127.0.0.1:1", "--reference", "http://127.0.0.1:1", "--timeout", "1").exit_code == 2
    assert cli("watch", "--rpc", "http://x.test").exit_code == 2  # --reference is required


def test_cli_transient_and_optional_failures_do_not_cause_non_zero(tmp_path):
    with FakeChain(generic_methods(block=10, chain_id=506)).serve() as a:
        result = cli("watch", "--rpc", a, "--reference", a, "--interval", "20ms", "--max-polls", "3")
    assert result.exit_code == 0  # eth_getBlockByNumber is unsupported here: SKIP, never a failure
