#!/usr/bin/env python3
"""Replay harness (docs/architecture.md §10, §10.1).

    prepare   Defects4J bug  ->  review task (inverse patch + executable truth)
    score     findings JSON  ->  localization metrics

Does NOT invoke the arms, so it stays neutral between them and runs without
either. Defects4J ships V_buggy and V_fixed differing only in source; diffing
fixed -> buggy yields a change that *introduces* a known bug, and a triggering
test proves the bug is real rather than merely annotated.

    python bench.py prepare --bugs Lang:1,Math:5 --out tasks/
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


def prepare_bug(pid: str, bid: str, work: Path) -> dict:
    fixed, buggy = work / f"{pid}-{bid}f", work / f"{pid}-{bid}b"
    for v, d in ((f"{bid}f", fixed), (f"{bid}b", buggy)):
        _sh(["defects4j", "checkout", "-p", pid, "-v", v, "-w", str(d)])
    src = _sh(["defects4j", "export", "-p", "dir.src.classes", "-w", str(fixed)]).strip()

    # fixed -> buggy INTRODUCES the bug. --no-index exits 1 on difference.
    diff = subprocess.run(["git", "diff", "--no-index", "--unified=3",
                           f"{fixed.name}/{src}", f"{buggy.name}/{src}"],
                          cwd=work, capture_output=True, text=True).stdout

    truth: dict[str, list[int]] = {}
    for h in parse_hunks(diff):
        p = h.path
        for pre in (buggy.name, fixed.name):            # drop the checkout dir
            if p.startswith(pre + "/"):
                p = p[len(pre) + 1:]
        truth.setdefault(p, []).extend(h.touched)

    return {"bug": f"{pid}-{bid}", "dataset": "defects4j",
            "base_checkout": str(fixed),      # graph builds here, as the hook would
            "head_checkout": str(buggy), "diff": diff,
            "ground_truth": {k: sorted(set(v)) for k, v in truth.items()},
            "triggering_tests": _sh(["defects4j", "export", "-p", "tests.trigger",
                                     "-w", str(buggy)]).split()}


def prepare_git(repo: Path, sha: str, work: Path) -> dict:
    """Build a task from a real fix commit -- no Defects4J needed.

    Same inversion as prepare_bug: diff the FIXED tree back to its parent, so the
    'PR' is the change that introduces the bug the commit fixed. Ground truth is
    what the fix touched. Weaker than Defects4J (no triggering test) but it runs
    against your own history, which §10 calls the decisive arm.
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--bugs", required=True, help="comma list, e.g. Lang:1,Math:5")
    p.add_argument("--out", type=Path, default=Path("tasks"))
    p.add_argument("--work", type=Path, default=Path(".d4j"))
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

    if a.cmd == "prepare":
        a.out.mkdir(parents=True, exist_ok=True)
        a.work.mkdir(parents=True, exist_ok=True)
        for spec in a.bugs.split(","):
            pid, bid = spec.strip().split(":")
            try:
                task = prepare_bug(pid, bid, a.work.resolve())
            except FileNotFoundError:
                print("defects4j not on PATH -- run setup-defects4j.sh", file=sys.stderr)
                return 2
            except subprocess.CalledProcessError as e:
                print(f"{pid}-{bid}: {e.stderr.strip()[:200]}", file=sys.stderr)
                continue
            dest = a.out / f"{pid}-{bid}.json"
            dest.write_text(json.dumps(task, indent=2), encoding="utf-8")
            print(f"{dest}  files={len(task['ground_truth'])}")
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
