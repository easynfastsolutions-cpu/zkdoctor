import io
import json

from conftest import FakeChain, era_methods, load, os_methods
from rich.console import Console
from typer.testing import CliRunner

from zkdoctor.cli import app
from zkdoctor.client import RpcClient
from zkdoctor.compare import compare_scans
from zkdoctor.probes import run_scan
from zkdoctor.report import render_comparison, render_scan

runner = CliRunner()
SECRET_PARTS = ("alice", "hunter2", "abcdefghijklmnopqrstuvwxyz123456", "SECRETVAL")


def scan_of(methods, url="http://node.test"):
    return run_scan(RpcClient(url, transport=FakeChain(methods).transport()))


def render(fn, *args, encoding="utf-8"):
    class Sink(io.StringIO):
        pass

    Sink.encoding = encoding  # type: ignore[assignment,misc]
    buf = Sink()
    console = Console(file=buf, width=100, force_terminal=False, color_system=None)
    fn(*args, console)
    return buf.getvalue()


def test_scan_report_shows_environment_checks_and_summary():
    text = render(render_scan, scan_of(os_methods(chain_id=270)))
    assert "ZKsync Compatibility Doctor" in text
    assert "Chain ID:          270" in text
    assert "zksync-os" in text
    assert "✓ RPC-001 chain ID" in text
    assert "ZKS-004 L1 batch number - unsupported" in text
    assert "Generic RPC       4/4 PASS" in text
    assert "ZKsync RPC        3/7 supported" in text
    assert "PASS: 7" in text and "SKIP: 4" in text


def test_scan_report_falls_back_to_ascii_on_narrow_terminals():
    text = render(render_scan, scan_of(os_methods()), encoding="cp1252")
    assert "PASS RPC-001" in text
    assert "✓" not in text and "─" not in text


def test_no_health_scores_in_terminal_output():
    text = render(render_scan, scan_of(os_methods()))
    assert "score" not in text.lower() and "%" not in text and "/100" not in text


def test_failure_details_are_listed():
    methods = os_methods()
    methods["zks_getGenesis"] = {k: v for k, v in load("genesis.json").items() if k != "genesis_root"}
    text = render(render_scan, scan_of(methods))
    assert "FAIL: 1" in text
    assert "missing required field 'genesis_root'" in text


def test_comparison_report_groups_by_impact():
    target = os_methods(client="zksync-os/0.5.0")
    target["zks_getGenesis"] = load("genesis_altered.json")
    text = render(render_comparison, compare_scans(scan_of(os_methods()), scan_of(target)))
    assert text.index("BREAKING") < text.index("WARNING")
    assert "field removed: execution_version" in text
    assert "client version changed" in text
    assert "UNCHANGED" in text


def test_scan_and_compare_output_redact_credentials():
    url = "https://alice:hunter2@node.test:8545/v2/abcdefghijklmnopqrstuvwxyz123456?apikey=SECRETVAL"
    scan = scan_of(os_methods(), url=url)
    text = render(render_scan, scan) + render(render_comparison, compare_scans(scan, scan))
    for secret in SECRET_PARTS:
        assert secret not in text
    assert "node.test:8545" in text


# ------------------------------------------------------------------- CLI


def run_cli(*args):
    return runner.invoke(app, list(args))


def test_help_screens_work():
    for args in (["--help"], ["scan", "--help"], ["compare", "--help"]):
        result = run_cli(*args)
        assert result.exit_code == 0
        assert "Usage" in result.output


def test_scan_writes_json_and_exits_zero(tmp_path):
    out = tmp_path / "scan.json"
    with FakeChain(os_methods()).serve() as url:
        result = run_cli("scan", "--rpc", url, "--output", str(out))
    assert result.exit_code == 0, result.output
    assert "Evidence:" in result.output
    data = json.loads(out.read_text())
    assert data["schema_version"] == "0.1"
    assert data["summary"]["fail"] == 0


def test_scan_exit_1_on_fail_and_json_stdout():
    methods = os_methods()
    methods["zks_getGenesis"] = {"initial_contracts": []}
    with FakeChain(methods).serve() as url:
        result = run_cli("scan", "--rpc", url, "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)["summary"]["fail"] == 1


def test_scan_require_flag_makes_missing_method_exit_1():
    with FakeChain(os_methods()).serve() as url:
        result = run_cli("scan", "--rpc", url, "--require", "zks_L1BatchNumber")
    assert result.exit_code == 1


def test_unreachable_endpoint_is_exit_2_not_a_compatibility_failure():
    result = run_cli("scan", "--rpc", "http://127.0.0.1:1", "--timeout", "2")
    assert result.exit_code == 2
    assert "cannot reach endpoint" in result.output


def test_invalid_arguments_exit_2():
    assert run_cli("scan").exit_code == 2
    assert run_cli("scan", "--rpc", "not-a-url").exit_code == 2
    assert run_cli("compare", "only-one.json").exit_code == 2


def test_cli_never_prints_credentials():
    url = "http://alice:hunter2@127.0.0.1:1/v2/abcdefghijklmnopqrstuvwxyz123456?apikey=SECRETVAL"
    result = run_cli("scan", "--rpc", url, "--timeout", "2")
    assert result.exit_code == 2
    for secret in SECRET_PARTS:
        assert secret not in result.output


def test_compare_end_to_end_exit_codes(tmp_path):
    base, same, broken = (tmp_path / n for n in ("base.json", "same.json", "broken.json"))
    altered = os_methods()
    altered["zks_getGenesis"] = load("genesis_altered.json")
    for path, methods in ((base, os_methods(block=10)), (same, os_methods(block=5000)), (broken, altered)):
        with FakeChain(methods).serve() as url:
            assert run_cli("scan", "--rpc", url, "--output", str(path)).exit_code in (0, 1)

    ok = run_cli("compare", str(base), str(same))
    assert ok.exit_code == 0, ok.output
    assert "No compatibility differences found" in ok.output

    out = tmp_path / "comparison.json"
    bad = run_cli("compare", str(base), str(broken), "--output", str(out))
    assert bad.exit_code == 1
    assert "field removed: execution_version" in bad.output
    assert json.loads(out.read_text())["summary"]["breaking"] == 1


def test_compare_rejects_unreadable_scan(tmp_path):
    junk = tmp_path / "junk.json"
    junk.write_text("nope")
    result = run_cli("compare", str(junk), str(junk))
    assert result.exit_code == 2


def test_compare_critical_option_escalates_capability_loss(tmp_path):
    base, target = tmp_path / "b.json", tmp_path / "t.json"
    lost = era_methods()
    del lost["zks_L1BatchNumber"]
    for path, methods in ((base, era_methods()), (target, lost)):
        with FakeChain(methods).serve() as url:
            run_cli("scan", "--rpc", url, "--output", str(path))
    assert run_cli("compare", str(base), str(target)).exit_code == 0
    assert run_cli("compare", str(base), str(target), "--critical", "zks_L1BatchNumber").exit_code == 1
