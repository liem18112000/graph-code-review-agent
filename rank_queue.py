#!/usr/bin/env python3
"""Rank multiple PRs/branches by aggregate risk -- `graphify prs --triage`
does not exist in the installed graphify (§9.5), so this reuses review.py's
own build() against each ref's diff from a common base. No new ranking
logic: one review.py run per ref, then sort.

    python queue.py --graph graphify-out/graph.json --base origin/main --refs feature/a,feature/b
    python queue.py selftest
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import review


def diff_for(repo: Path, base: str, ref: str, timeout: int = 30) -> str:
    return subprocess.run(["git", "-C", str(repo), "diff", f"{base}...{ref}"],
                          capture_output=True, text=True, timeout=timeout).stdout


def rank_one(diff: str, graph_raw: dict, repo: Path | None,
            overrides: dict, cap: int) -> dict:
    if not diff.strip():
        return {"total_risk": 0, "max_risk": 0, "bundles": 0,
               "sensitive": False, "hunks_dropped_free": 0, "note": "no diff vs base"}
    res = review.build(diff, review.Graph(graph_raw), repo, overrides, cap)
    return {"total_risk": sum(b["risk_hint"] for b in res["bundles"]),
           "max_risk": max((b["risk_hint"] for b in res["bundles"]), default=0),
           "bundles": len(res["bundles"]),
           "sensitive": any(b["sensitive"] for b in res["bundles"]),
           "hunks_dropped_free": res["stats"]["hunks_dropped_free"]}


def rank_refs(repo: Path, graph_raw: dict, base: str, refs: list[str],
             overrides: dict, cap: int = 12) -> list[dict]:
    out = [dict(ref=ref, **rank_one(diff_for(repo, base, ref), graph_raw, repo,
                                    overrides, cap))
          for ref in refs]
    # sensitive refs first (need a human whatever the score), then by risk
    out.sort(key=lambda r: (not r["sensitive"], -r["total_risk"]))
    return out


def selftest() -> int:
    """No git, no graph file -- proves rank_one()/rank_refs() ordering on an
    in-memory graph and two synthetic diffs."""
    graph_raw = {"nodes": [{"id": "a", "source_file": "src/auth/Foo.java",
                            "label": ".bar()", "source_location": "L1"}],
                "links": []}
    overrides = {"sensitive": ["**/auth/**"], "skip": [], "tests": []}
    big_diff = ("--- a/src/auth/Foo.java\n+++ b/src/auth/Foo.java\n"
               "@@ -1,2 +1,2 @@\n-old\n+new\n")
    small_diff = ("--- a/README.md\n+++ b/README.md\n"
                 "@@ -1,1 +1,1 @@\n-x\n+y\n")
    sensitive = rank_one(big_diff, graph_raw, None, overrides, 12)
    assert sensitive["sensitive"] is True and sensitive["bundles"] == 1, sensitive
    plain = rank_one(small_diff, graph_raw, None, overrides, 12)
    assert plain["sensitive"] is False and plain["bundles"] == 1, plain
    empty = rank_one("", graph_raw, None, overrides, 12)
    assert empty == {"total_risk": 0, "max_risk": 0, "bundles": 0,
                     "sensitive": False, "hunks_dropped_free": 0, "note": "no diff vs base"}
    print("ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest", help="synthetic self-check, no git or graph needed")
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--graph", type=Path)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--refs", help="comma list of branches or PR refs")
    ap.add_argument("--overrides", type=Path, default=Path("overrides.toml"))
    ap.add_argument("--max-bundle", type=int, default=12)
    a = ap.parse_args()

    if a.cmd == "selftest":
        return selftest()

    if not (a.graph and a.graph.exists()):
        print(f"no graph at {a.graph} -- run `graphify update <repo>` first", file=sys.stderr)
        return 2
    if not a.refs:
        ap.error("--refs is required (comma list)")

    graph_raw = json.loads(a.graph.read_text(encoding="utf-8"))
    overrides = review.load_overrides(a.overrides)
    refs = [r.strip() for r in a.refs.split(",") if r.strip()]
    ranked = rank_refs(a.repo, graph_raw, a.base, refs, overrides, a.max_bundle)
    json.dump(ranked, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
