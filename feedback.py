#!/usr/bin/env python3
"""Log human accept/dismiss per finding, and summarise it (§8 "Feedback
loop" -- the only honest source of a quality number, and the only thing
that calibrates tier 1 to this codebase). Append-only JSONL, no server.

    python agent.py --bundles bundles.json > findings.json
    python feedback.py log --findings findings.json --index 2 --verdict accepted
    python feedback.py log --file a.py --line 10 --verdict dismissed --note "stale caller count"
    python feedback.py stats
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


def record(finding: dict, human: str, note: str) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file": finding.get("file"), "line": finding.get("line"),
        "category": finding.get("category"), "severity": finding.get("severity"),
        "scope": finding.get("scope"), "confidence": finding.get("confidence"),
        # the field that actually calibrates tier 1 (§4.2): was this verdict
        # system 1's own choice, or the fallback severity mapping?
        "verdict": finding.get("verdict"), "verdict_by": finding.get("verdict_by"),
        "human": human, "note": note,
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


def selftest() -> int:
    """No real findings.json needed -- proves record()/append()/load()/
    summarise() round-trip through a throwaway temp log."""
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "feedback.jsonl"
        f1 = {"file": "a.py", "line": 10, "category": "correctness",
              "severity": "blocker", "verdict": "block", "verdict_by": "system1"}
        f2 = {"file": "b.py", "line": 20, "category": "complexity",
              "severity": "nitpick", "verdict": "note", "verdict_by": "fallback:low-confidence"}
        append(log, record(f1, "accepted", ""))
        append(log, record(f2, "dismissed", "not actually dead code"))
        rows = load(log)
        assert len(rows) == 2
        assert rows[1]["note"] == "not actually dead code"
        s = summarise(rows)
        assert s["total"] == 2 and s["accept_rate"] == 0.5, s
        assert s["by_category"]["correctness"] == 1.0
        assert s["by_category"]["complexity"] == 0.0
        assert s["by_verdict_by"]["system1"] == 1.0
        assert s["by_verdict_by"]["fallback:low-confidence"] == 0.0
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
    lg.add_argument("--findings", type=Path, help="findings.json to pull a finding from")
    lg.add_argument("--index", type=int, help="index into findings.json's findings array")
    lg.add_argument("--file", help="used only without --findings")
    lg.add_argument("--line", type=int)
    lg.add_argument("--category")
    lg.add_argument("--severity")

    st = sub.add_parser("stats")
    st.add_argument("--log", type=Path, default=DEFAULT_LOG)

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
        append(a.log, record(finding, a.verdict, a.note))
        print(f"logged {a.verdict} for {finding.get('file')}:{finding.get('line')}")
        return 0

    if a.cmd == "stats":
        print(json.dumps(summarise(load(a.log)), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
