#!/usr/bin/env python3
"""Log human accept/dismiss per finding, and summarise it (§8 "Feedback
loop" -- the only honest source of a quality number, and the only thing
that calibrates tier 1 to this codebase). Append-only JSONL, no server.

    python agent.py --bundles bundles.json > findings.json
    python feedback.py log --findings findings.json --index 2 --verdict accepted
    python feedback.py log --file a.py --line 10 --verdict dismissed --note "stale caller count"
    python feedback.py stats
    python feedback.py export-eval --out eval.jsonl   # see "Honest limits" below

Honest limits on `export-eval`: the installed `laya` package has no weight
fine-tuning entry point at all (checked -- only `laya.calibrate`, which needs
raw logits our HTTP integration never sees, and `laya-evals`, which evaluates
a checkpoint against a labelled dataset). `export-eval` builds that dataset,
in the `{"state", "questions", "expected"}` shape `laya-evals run` wants -- it
does not fine-tune or calibrate anything itself, and `laya.calibrate`'s own
per-bucket floor (2000 examples) is far more than this log will hold for a
long time. This is the measurement half of "fine-tune on PR history" (§4.1),
not the fine-tuning itself.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOG = Path(__file__).with_name("feedback.jsonl")
VERDICTS = ("accepted", "dismissed")


def record(finding: dict, human: str, note: str, expected: str | None = None) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file": finding.get("file"), "line": finding.get("line"),
        "category": finding.get("category"), "severity": finding.get("severity"),
        "scope": finding.get("scope"), "confidence": finding.get("confidence"),
        # the field that actually calibrates tier 1 (§4.2): was this verdict
        # system 1's own choice, or the fallback severity mapping?
        "verdict": finding.get("verdict"), "verdict_by": finding.get("verdict_by"),
        "human": human, "note": note,
        # the exact record tier 1 was asked about (system1.verdict()'s `rec`,
        # threaded through agent.py's adjudicate()) and what the *correct*
        # disposition actually was -- without both, this row cannot become a
        # labelled laya-evals example (export-eval), only a satisfaction count
        "tier1_state": finding.get("tier1_state"),
        "expected": expected,
    }


def append(log: Path, rec: dict) -> None:
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def load(log: Path) -> list[dict]:
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def summarise(rows: list[dict]) -> dict:
    def rate(group: list[dict]) -> float | None:
        return round(sum(r["human"] == "accepted" for r in group) / len(group), 3) \
            if group else None

    by = lambda key: {k: rate(g) for k, g in
                      _groupby(rows, key).items()}
    return {"total": len(rows), "accept_rate": rate(rows),
            "by_category": by("category"), "by_severity": by("severity"),
            "by_verdict_by": by("verdict_by")}


def _groupby(rows: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[str(r.get(key))].append(r)
    return out


def eval_examples(rows: list[dict]) -> list[dict]:
    """Rows with a usable label, in laya-evals' `Example` shape (state,
    questions, expected). Only rows with `tier1_state` qualify -- that's
    only set when agent.py's adjudicate() actually called tier 1 (not
    --no-verdict, not a lint finding). The expected choice is the recorded
    verdict when a human accepted it (confirmed correct), or the explicit
    `--expected` override when they dismissed it with a correction; a
    dismissal with no `--expected` says a verdict was wrong without saying
    what right looks like, so it cannot label an example and is skipped."""
    import system1
    out = []
    for r in rows:
        state = r.get("tier1_state")
        if not state:
            continue
        choice = r.get("expected") or (r.get("verdict") if r.get("human") == "accepted" else None)
        if choice not in ("block", "fix", "note"):
            continue
        out.append({"state": state, "questions": {system1.Q_VERDICT: system1.VERDICT_Q[system1.Q_VERDICT]},
                   "expected": {system1.Q_VERDICT: choice}})
    return out


def selftest() -> int:
    """No real findings.json needed -- proves record()/append()/load()/
    summarise()/eval_examples() round-trip through a throwaway temp log."""
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "feedback.jsonl"
        state_a = {"severity_claimed": "blocker", "scope": "introduced",
                  "reviewer_confidence": 0.9, "summary": "x", "failure": "y",
                  "sensitive_path": "no"}
        f1 = {"file": "a.py", "line": 10, "category": "correctness",
              "severity": "blocker", "verdict": "block", "verdict_by": "system1",
              "tier1_state": state_a}
        f2 = {"file": "b.py", "line": 20, "category": "complexity",
              "severity": "nitpick", "verdict": "note", "verdict_by": "fallback:low-confidence"}
        f3 = {"file": "c.py", "line": 30, "category": "correctness",
              "severity": "blocker", "verdict": "block", "verdict_by": "system1",
              "tier1_state": {**state_a, "summary": "z"}}
        append(log, record(f1, "accepted", ""))                      # labels block=correct
        append(log, record(f2, "dismissed", "not actually dead code"))  # no tier1_state -> unusable
        append(log, record(f3, "dismissed", "wrong", expected="note"))  # labels note=correct
        rows = load(log)
        assert len(rows) == 3
        assert rows[1]["note"] == "not actually dead code"
        s = summarise(rows)
        assert s["total"] == 3 and round(s["accept_rate"], 3) == round(1 / 3, 3), s
        assert s["by_category"]["correctness"] == 0.5    # f1 accepted, f3 dismissed
        assert s["by_category"]["complexity"] == 0.0
        assert s["by_verdict_by"]["system1"] == 0.5       # f1 accepted, f3 dismissed
        assert s["by_verdict_by"]["fallback:low-confidence"] == 0.0

        import system1
        examples = eval_examples(rows)
        assert len(examples) == 2, examples          # f2 skipped: no tier1_state
        assert examples[0]["state"] == state_a
        assert examples[0]["expected"][system1.Q_VERDICT] == "block"
        assert examples[1]["expected"][system1.Q_VERDICT] == "note"
        assert examples[0]["questions"] == {system1.Q_VERDICT: system1.VERDICT_Q[system1.Q_VERDICT]}
    print("ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="round-trips a temp log, no fixtures needed")

    lg = sub.add_parser("log")
    lg.add_argument("--log", type=Path, default=DEFAULT_LOG)
    lg.add_argument("--verdict", required=True, choices=VERDICTS)
    lg.add_argument("--note", default="")
    lg.add_argument("--expected", choices=("block", "fix", "note"),
                    help="the correct disposition, when --verdict dismissed means "
                         "tier 1's choice was wrong -- without this, a dismissal "
                         "cannot become a labelled export-eval example (see --help)")
    lg.add_argument("--findings", type=Path, help="findings.json to pull a finding from")
    lg.add_argument("--index", type=int, help="index into findings.json's findings array")
    lg.add_argument("--file", help="used only without --findings")
    lg.add_argument("--line", type=int)
    lg.add_argument("--category")
    lg.add_argument("--severity")

    st = sub.add_parser("stats")
    st.add_argument("--log", type=Path, default=DEFAULT_LOG)

    ev = sub.add_parser("export-eval", help="write a laya-evals dataset from labelled rows")
    ev.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ev.add_argument("--out", type=Path, required=True)

    a = ap.parse_args()

    if a.cmd == "selftest":
        return selftest()

    if a.cmd == "log":
        if a.findings:
            if a.index is None:
                ap.error("--findings needs --index")
            findings = json.loads(a.findings.read_text(encoding="utf-8"))["findings"]
            if not (0 <= a.index < len(findings)):
                print(f"index {a.index} out of range (0..{len(findings) - 1})", file=sys.stderr)
                return 2
            finding = findings[a.index]
        else:
            finding = {"file": a.file, "line": a.line, "category": a.category,
                       "severity": a.severity}
        append(a.log, record(finding, a.verdict, a.note, a.expected))
        print(f"logged {a.verdict} for {finding.get('file')}:{finding.get('line')}")
        return 0

    if a.cmd == "stats":
        print(json.dumps(summarise(load(a.log)), indent=2))
        return 0

    if a.cmd == "export-eval":
        examples = eval_examples(load(a.log))
        with a.out.open("w", encoding="utf-8") as f:
            for ex in examples:
                f.write(json.dumps(ex) + "\n")
        skipped = len(load(a.log)) - len(examples)
        print(f"{len(examples)} labelled example(s) written to {a.out} "
              f"({skipped} row(s) skipped -- no tier1_state or no usable expected choice)",
              file=sys.stderr)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
