"""Experiment helper (not part of the zkdoctor package): print raw side-by-side facts for two
scans and the comparison, as Markdown. Standard library only. Makes no judgement: the
classification of differences is done by a human reading this output.

usage: summarize.py scan-A.json scan-B.json comparison.json
"""

import json
import sys
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cell(value):
    text = "-" if value is None else str(value)
    return text.replace("|", "\\|")


def main(a_path, b_path, cmp_path):
    a, b = load(a_path), load(b_path)
    out = ["## Environment (A = baseline, B = target)", "", "| field | A | B |", "|---|---|---|"]
    for key in ("chain_id", "net_version", "client_version", "detected_execution_environment", "execution_version"):
        out.append(f"| {key} | {cell(a['environment'].get(key))} | {cell(b['environment'].get(key))} |")
    for key in sorted(set(a["environment"]["protocol_metadata"]) | set(b["environment"]["protocol_metadata"])):
        out.append(
            f"| protocol_metadata.{key} | {cell(a['environment']['protocol_metadata'].get(key))} "
            f"| {cell(b['environment']['protocol_metadata'].get(key))} |"
        )

    out += ["", "## Capabilities", "", "| method | A | B |", "|---|---|---|"]
    for method in sorted(set(a["capabilities"]) | set(b["capabilities"])):
        ca, cb = a["capabilities"].get(method, {}), b["capabilities"].get(method, {})
        out.append(f"| {method} | {cell(ca.get('supported'))} | {cell(cb.get('supported'))} |")

    out += ["", "## Probes", "", "| probe | A status | B status | A details | B details |", "|---|---|---|---|---|"]
    ra = {r["probe_id"]: r for r in a["results"]}
    rb = {r["probe_id"]: r for r in b["results"]}
    for pid in sorted(set(ra) | set(rb)):
        x, y = ra.get(pid, {}), rb.get(pid, {})
        out.append(
            f"| {pid} {x.get('name', '')} | {cell(x.get('status'))} | {cell(y.get('status'))} "
            f"| {cell('; '.join(x.get('details', [])))} | {cell('; '.join(y.get('details', [])))} |"
        )

    out += ["", "## Response shapes (per probe, where they differ)", ""]
    shown = False
    for pid in sorted(set(ra) & set(rb)):
        sa = ((ra[pid].get("evidence") or {}).get("result_summary") or {}).get("shape")
        sb = ((rb[pid].get("evidence") or {}).get("result_summary") or {}).get("shape")
        if sa != sb:
            shown = True
            out += [f"**{pid}**", "```", f"A: {json.dumps(sa)}", f"B: {json.dumps(sb)}", "```"]
    if not shown:
        out.append("(none)")

    cmp = load(cmp_path)
    out += ["", "## zkdoctor compare output (raw)", "", f"summary: `{json.dumps(cmp['summary'])}`", ""]
    out += ["| impact | change | subject | path | message | A | B |", "|---|---|---|---|---|---|---|"]
    for d in cmp["differences"]:
        out.append(
            f"| {d['impact']} | {d['change']} | {cell(d['subject'])} | {cell(d.get('path'))} | {cell(d['message'])} "
            f"| {cell(d.get('baseline'))} | {cell(d.get('target'))} |"
        )
    if not cmp["differences"]:
        out.append("| (none) | | | | | | |")
    out.append(f"\nunchanged probes: {', '.join(cmp['unchanged_probes']) or '(none)'}")
    print("\n".join(out))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
