#!/usr/bin/env python3
"""Merge two findings lists (e.g. this plugin's graph review and Claude's own
`/code-review`) into one report: found by both, found only by each side.

All three real comparisons run against this plugin (docs/architecture.md §10)
independently concluded the same thing: the two methods have near-zero
overlap and complementary blind spots, so running both finds more than
either alone. This formalises that into a repeatable step instead of a
by-hand comparison each time.

    python agent.py --bundles bundles.json --repo . > graph.json
    # ask Claude to run /code-review on the same diff and save its findings
    # as {"findings": [{"file", "line", "summary", ...}, ...]} -- same shape
    # graph.json already uses -- to code_review.json, then:
    python merge_findings.py --a graph.json --a-label graph \
                              --b code_review.json --b-label code-review \
                              > merged.json
    python merge_findings.py selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from diffparse import norm


def _findings(doc: dict | list) -> list[dict]:
    return doc.get("findings", []) if isinstance(doc, dict) else doc


def _same_file(a: str, b: str) -> bool:
    a, b = norm(str(a)), norm(str(b))
    return a == b or a.endswith(b) or b.endswith(a)


def _line(f: dict) -> int | None:
    try:
        return int(f.get("line"))
    except (TypeError, ValueError):
        return None


def merge(a: list[dict], b: list[dict], a_label: str, b_label: str,
         tolerance: int = 3) -> dict:
    """Greedy one-to-one matching: each `b` finding matches at most one `a`
    finding, same file (suffix-wise, like bench.py's ground-truth match) and
    within `tolerance` lines. Order-independent, not content-aware -- two
    findings on the same line about different things still count as a
    match, same limitation bench.py's scorer accepts (§10)."""
    used_b = set()
    both, only_a = [], []
    for fa in a:
        la = _line(fa)
        match = None
        if la is not None:
            for j, fb in enumerate(b):
                if j in used_b:
                    continue
                lb = _line(fb)
                if lb is not None and _same_file(fa.get("file", ""), fb.get("file", "")) \
                        and abs(la - lb) <= tolerance:
                    match = j
                    break
        if match is not None:
            used_b.add(match)
            both.append({a_label: fa, b_label: b[match]})
        else:
            only_a.append(fa)
    only_b = [fb for j, fb in enumerate(b) if j not in used_b]
    return {"found_by_both": both,
           f"only_{a_label}": only_a, f"only_{b_label}": only_b,
           "counts": {a_label: len(a), b_label: len(b), "both": len(both),
                     f"only_{a_label}": len(only_a), f"only_{b_label}": len(only_b)}}


def selftest() -> int:
    """Synthetic, no real findings files needed."""
    a = [{"file": "src/a.py", "line": 10, "summary": "x"},
        {"file": "pkg/b.py", "line": 50, "summary": "y"}]
    b = [{"file": "a.py", "line": 12, "summary": "x again"},      # matches a[0], suffix+tolerance
        {"file": "c.py", "line": 1, "summary": "z"}]              # matches nothing
    r = merge(a, b, "graph", "cr", tolerance=3)
    assert r["counts"] == {"graph": 2, "cr": 2, "both": 1, "only_graph": 1, "only_cr": 1}, r["counts"]
    assert r["found_by_both"][0]["graph"]["summary"] == "x"
    assert r["only_graph"][0]["summary"] == "y"
    assert r["only_cr"][0]["summary"] == "z"
    print("ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest", help="synthetic self-check, no files needed")
    ap.add_argument("--a", type=Path)
    ap.add_argument("--b", type=Path)
    ap.add_argument("--a-label", default="a")
    ap.add_argument("--b-label", default="b")
    ap.add_argument("--tolerance", type=int, default=3)
    args = ap.parse_args()

    if args.cmd == "selftest":
        return selftest()
    if not (args.a and args.b):
        ap.error("need --a and --b, or selftest")

    a = _findings(json.loads(args.a.read_text(encoding="utf-8")))
    b = _findings(json.loads(args.b.read_text(encoding="utf-8")))
    result = merge(a, b, args.a_label, args.b_label, args.tolerance)
    json.dump(result, sys.stdout, indent=2)
    print()
    print(f"{result['counts']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
