#!/usr/bin/env python3
"""Replay harness (docs/architecture.md §10, §10.1).

    prepare-git   fix commit  ->  review task (inverse patch, no Defects4J needed)
    score         findings JSON  ->  localization metrics

Does NOT invoke the arms, so it stays neutral between them and runs without
either. `prepare-git` diffs a fix commit back to its parent, so the "PR" is
the change that *re-introduces* the bug that commit fixed, against your own
history -- §10 calls this the decisive arm.

    python bench.py prepare-git --repo . --shas <sha1>,<sha2> --out tasks/
    python bench.py score --tasks-dir tasks/ --findings-dir out/graph/
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from diffparse import norm, parse_hunks


def _sh(cmd: list[str], **kw) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def prepare_git(repo: Path, sha: str, work: Path) -> dict:
    """Build a task from a real fix commit -- no Defects4J needed.

    Diff the FIXED tree back to its parent, so the 'PR' is the change that
    introduces the bug the commit fixed. Ground truth is what the fix
    touched. Weaker than an executable benchmark (no triggering test) but it
    runs against your own history, which §10 calls the decisive arm.
    """
    g = lambda *a: _sh(["git", "-C", str(repo)] + list(a))
    subject = g("log", "-1", "--format=%s", sha).strip()
    # fix -> parent: applying this re-introduces the bug
    diff = subprocess.run(["git", "-C", str(repo), "diff", f"{sha}..{sha}~1",
                           "--", "*.java"], capture_output=True, text=True).stdout

    truth: dict[str, list[int]] = {}
    for h in parse_hunks(diff):
        truth.setdefault(h.path, []).extend(h.touched)

    return {"bug": sha[:9], "dataset": "git-history", "subject": subject,
            "base_sha": sha, "head_sha": sha + "~1", "diff": diff,
            "ground_truth": {k: sorted(set(v)) for k, v in truth.items()},
            "triggering_tests": []}


def score_one(task: dict, findings: list[dict], tol: int = 5) -> dict:
    """Rank matters: reviews are read top-down, so a hit under nine false
    positives is worth less. Path match is suffix-wise both ways -- generous,
    erring toward crediting the arm over a formatting difference."""
    truth = task["ground_truth"]
    hits = []
    for i, f in enumerate(findings):
        p = norm(str(f.get("file", "")))
        lines = next((v for k, v in truth.items()
                      if k == p or p.endswith(k) or k.endswith(p)), None)
        try:
            ln = int(f.get("line", -1))
        except (TypeError, ValueError):
            continue
        if lines and any(abs(ln - t) <= tol for t in lines):
            hits.append(i)
    return {"bug": task["bug"], "localized": bool(hits),
            "rank_of_first_hit": hits[0] + 1 if hits else None,
            "findings_total": len(findings), "findings_hit": len(hits)}


def aggregate(rows: list[dict]) -> dict:
    n = len(rows) or 1
    loc = [r for r in rows if r["localized"]]
    total = sum(r["findings_total"] for r in rows)
    return {"bugs": len(rows),
            "localization_rate": round(len(loc) / n, 3),
            "mean_findings_per_bug": round(total / n, 2),
            "precision_proxy": round(sum(r["findings_hit"] for r in rows) / (total or 1), 3),
            "mean_rank_of_first_hit": round(
                sum(r["rank_of_first_hit"] for r in loc) / len(loc), 2) if loc else None}


def _load(p: Path, key: str | None = None):
    d = json.loads(p.read_text(encoding="utf-8"))
    return d.get(key, []) if key and isinstance(d, dict) else d


def selftest() -> int:
    """No defects4j, no network, no filesystem writes -- proves score_one()
    and aggregate() on synthetic data, so a regression there fails loudly
    instead of waiting for a real benchmark run to notice."""
    task = {"bug": "X-1", "ground_truth": {"a/Foo.java": [10, 11, 40]}}
    hit = score_one(task, [{"file": "a/Foo.java", "line": 12}], tol=5)
    assert hit == {"bug": "X-1", "localized": True, "rank_of_first_hit": 1,
                   "findings_total": 1, "findings_hit": 1}, hit
    miss = score_one(task, [{"file": "a/Foo.java", "line": 100}], tol=5)
    assert miss["localized"] is False and miss["rank_of_first_hit"] is None, miss
    suffix = score_one(task, [{"file": "pkg/a/Foo.java", "line": 10}], tol=0)
    assert suffix["localized"] is True, "suffix-wise path match should still hit"
    agg = aggregate([hit, miss])
    assert agg["bugs"] == 2 and agg["localization_rate"] == 0.5, agg
    print("ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="synthetic self-check, no network needed")
    q = sub.add_parser("prepare-git", help="tasks from real fix commits in a repo")
    q.add_argument("--repo", type=Path, required=True)
    q.add_argument("--shas", required=True, help="comma list of fix-commit SHAs")
    q.add_argument("--out", type=Path, default=Path("tasks"))
    s = sub.add_parser("score")
    s.add_argument("--task", type=Path)
    s.add_argument("--findings", type=Path)
    s.add_argument("--tasks-dir", type=Path)
    s.add_argument("--findings-dir", type=Path)
    s.add_argument("--tolerance", type=int, default=5)
    a = ap.parse_args()

    if a.cmd == "selftest":
        return selftest()

    if a.cmd == "prepare-git":
        a.out.mkdir(parents=True, exist_ok=True)
        for sha in a.shas.split(","):
            t = prepare_git(a.repo, sha.strip(), a.out)
            if not t["ground_truth"]:
                print(f"{sha}: no java hunks, skipped", file=sys.stderr)
                continue
            (a.out / f"{t['bug']}.json").write_text(json.dumps(t, indent=2),
                                                    encoding="utf-8")
            print(f"{t['bug']}  files={len(t['ground_truth'])}  {t['subject'][:58]}")
        return 0

    if a.tasks_dir:
        if not a.findings_dir:
            ap.error("--tasks-dir needs --findings-dir")
        # a missing findings file is a MISS, never a skip: an arm that crashes
        # must not be flattered by its own failure
        rows = []
        for t in sorted(a.tasks_dir.glob("*.json")):
            f = a.findings_dir / t.name
            rows.append(score_one(_load(t), _load(f, "findings") if f.exists() else [],
                                  a.tolerance))
        print(json.dumps({"per_bug": rows, **aggregate(rows)}, indent=2))
        return 0

    if not (a.task and a.findings):
        ap.error("need --task/--findings, or --tasks-dir/--findings-dir")
    print(json.dumps(score_one(_load(a.task), _load(a.findings, "findings"),
                               a.tolerance), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
