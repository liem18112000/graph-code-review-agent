#!/usr/bin/env python3
"""diff -> hunks -> graph facts -> ranked bundles (§3, §6).

    git diff origin/main... | python review.py --graph graphify-out/graph.json --repo .

Measured constraints (§9): paths join by SUFFIX (graph points at a staging
copy); 36% of nodes have no line number and degrade to file level; fan_in 0 is
UNKNOWN, never safe, so entry status is scanned from SOURCE.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from pathlib import Path

from diffparse import Hunk, norm, parse_hunks

ENTRY_RE = re.compile(r"@(Path|GET|POST|PUT|DELETE|PATCH|Observes\w*|Scheduled"
                      r"|ConsumeEvent|Incoming|PostConstruct|Startup)\b")
LINE_RE = re.compile(r"L(\d+)")
UNKNOWN = "unknown"
KINDS = ("sensitive", "skip", "tests")


DEFAULT_OVERRIDES = Path(__file__).with_name("overrides.toml")


def _load_toml(p: Path | None) -> dict:
    # fall back to the copy shipped beside this script: a target repo usually
    # has no overrides.toml of its own, and empty globs silently disable tier 0
    if not (p and p.exists()):
        p = DEFAULT_OVERRIDES if DEFAULT_OVERRIDES.exists() else None
    return tomllib.loads(p.read_text(encoding="utf-8")) if p and p.exists() else {}


def load_overrides(p: Path | None) -> dict[str, list[str]]:
    raw = _load_toml(p)
    return {k: raw.get(k, {}).get("globs", []) for k in KINDS}


def load_linters(p: Path | None) -> list[dict]:
    """Opt-in (`--run-linters`) tier-0 SAST/lint wiring (§8 "Deterministic
    stays deterministic" -- most pipelines assume CI already ran these;
    this lets a bare `review.py` run do it too, still at zero tokens)."""
    return [c for c in _load_toml(p).get("linters", []) if c.get("enabled", True)]


def run_linters(repo: Path | None, paths: set[str], linters: list[dict],
                timeout: int = 30) -> list[dict]:
    """Each configured command must emit a JSON array of
    `{"path", "line", "severity", "message"}` to stdout -- that is OUR
    contract, not any one vendor's, so a tool whose native output differs
    needs a thin wrapper. Never raises: a broken linter config must not sink
    the review that already succeeded."""
    out = []
    for cfg in linters:
        name = cfg.get("name", "?")
        matched = sorted(p for p in paths if hit(p, [cfg.get("glob", "**")]))
        if not matched or not cfg.get("cmd"):
            continue
        try:
            proc = subprocess.run(list(cfg["cmd"]) + matched, cwd=repo,
                                  capture_output=True, text=True, timeout=timeout)
            items = json.loads(proc.stdout or "[]")
        except Exception as e:                  # noqa: BLE001 -- report, never crash
            print(f"linter {name}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for it in items:
            if isinstance(it, dict) and "path" in it:
                out.append({"file": it["path"], "line": it.get("line"),
                           "category": "lint", "severity": it.get("severity", "info"),
                           "scope": "introduced", "summary": it.get("message", ""),
                           "source": f"lint:{name}"})
    return out


def hit(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


def graph_freshness(raw: dict, repo: Path | None) -> dict:
    """§8 'Graph freshness' was a manual step in the skill, not enforced in
    code -- a stale graph degrades every downstream signal with NO error.
    graphify stamps `built_at_commit`; compare it to the repo's actual HEAD.
    Never raises: a freshness check must not be why a review fails to run."""
    built = raw.get("built_at_commit")
    if not built or not repo:
        return {"checked": False, "built_at_commit": built, "head_commit": None,
                "stale": None}
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return {"checked": False, "built_at_commit": built, "head_commit": None,
                "stale": None}
    return {"checked": True, "built_at_commit": built, "head_commit": head or None,
            "stale": bool(head) and head != built}


def line(n: dict) -> int | None:
    m = LINE_RE.fullmatch(str(n.get("source_location") or "").strip())
    return int(m.group(1)) if m else None


class Graph:
    def __init__(self, raw: dict):
        self.by_id = {n["id"]: n for n in raw["nodes"]}
        self.by_base: dict[str, list[dict]] = defaultdict(list)
        for n in raw["nodes"]:
            if p := norm(str(n.get("source_file") or "")):
                n["_p"] = p
                self.by_base[p.rsplit("/", 1)[-1]].append(n)
        self.fan_in: Counter = Counter()
        self.callees: dict[str, list[str]] = defaultdict(list)
        self.incoming: dict[str, list[str]] = defaultdict(list)
        # clustered graphs key this "edges"; raw --no-cluster extractions use
        # the D3-style "links". Accept either or the whole graph looks empty.
        for e in (raw.get("edges") or raw.get("links") or []):
            if e.get("confidence") != "EXTRACTED":      # §9.4 found edges only
                continue
            if e["relation"] == "calls":
                self.fan_in[e["target"]] += 1
                self.callees[e["source"]].append(e["target"])
            if e["relation"] in ("calls", "references"):
                self.incoming[e["target"]].append(e["source"])

    def in_file(self, path: str) -> list[dict]:
        return [n for n in self.by_base.get(path.rsplit("/", 1)[-1], [])
                if n["_p"].endswith(path)]

    def nodes_for(self, h: Hunk) -> tuple[list[dict], str]:
        f = self.in_file(h.path)
        if not f:
            return [], "none"
        p = [n for n in f if (ln := line(n)) and h.start <= ln <= h.end]
        return (p, "line") if p else (f, "file")

    def tests_for(self, paths: set[str], globs: list[str]) -> list[str]:
        """File level: test->source edges land on the CLASS node, never the
        method, so a per-symbol lookup finds nothing."""
        out = set()
        for p in paths:
            for n in self.in_file(p):
                for s in self.incoming.get(n["id"], []):
                    sp = self.by_id.get(s, {}).get("_p")
                    if sp and hit(sp, globs):
                        out.add(sp.split("/src/")[-1] if "/src/" in sp else sp)
        return sorted(out)

    def sensitive_ids(self, globs: list[str]) -> set[str]:
        return {nid for nid, n in self.by_id.items() if hit(n.get("_p") or "", globs)}

    def hops_to_sensitive(self, start_ids: list[str], sensitive_ids: set[str],
                          test_globs: list[str], max_hops: int = 4) -> int | None:
        """§9.3: the original `graphify path` walked all relation types,
        undirected, through test files, which is noise. This instead walks
        only directed `calls` edges (`self.callees`, already EXTRACTED-only),
        excludes test files, and caps depth -- a bounded BFS, not a path
        search, so it is cheap even on a large graph."""
        if not sensitive_ids or not start_ids:
            return None
        seen = set(start_ids)
        frontier = [i for i in start_ids if i not in sensitive_ids]
        for hop in range(1, max_hops + 1):
            nxt = []
            for nid in frontier:
                for c in self.callees.get(nid, ()):
                    if c in seen:
                        continue
                    seen.add(c)
                    cn = self.by_id.get(c)
                    if cn and hit(cn.get("_p") or "", test_globs):
                        continue                 # a test calling in does not count
                    if c in sensitive_ids:
                        return hop
                    nxt.append(c)
            frontier = nxt
            if not frontier:
                break
        return None


def entries(repo: Path | None, h: Hunk) -> list[str]:
    """Entry annotations from SOURCE -- the wiring the graph cannot see (§9.2)."""
    f = repo / h.path if repo else None
    if not (f and f.is_file()):
        return []
    src = f.read_text(encoding="utf-8", errors="replace").splitlines()
    return sorted({m.group(0) for m in
                   ENTRY_RE.finditer("\n".join(src[max(0, h.start - 40):h.end]))})


def rank(b: dict) -> tuple[int, list[str]]:
    """Deterministic tier-0 prior, NOT a model score; tier 1 reorders later."""
    score, why = 0, []

    def add(n: int, msg: str) -> None:
        nonlocal score
        score += n
        why.append(f"{msg} (+{n})")

    syms = b["facts"]["symbols"]
    known = [s["callers"] for s in syms if s["callers"] != UNKNOWN]
    if b["sensitive"]:
        add(100, "sensitive path")
    if e := b["facts"]["entry_point_annotations"]:
        add(25, "entry " + ",".join(e[:3]))
    if u := sum(s["callers"] == UNKNOWN for s in syms):
        add(10, f"{u} symbol(s) unknown fan-in")
    if known:
        add(min(max(known), 25), f"max fan-in {max(known)}")
    if syms and not b["facts"]["covering_tests"]:
        add(10, "no covering tests")
    if not b["facts"]["in_graph"]:
        add(5, "not in graph")
    if (hops := b["facts"]["path_to_sensitive_hops"]) is not None:
        add(max(10, 50 - hops * 10), f"path to sensitive in {hops} hop(s)")
    add(len(b["hunks"]), f"{len(b['hunks'])} hunk(s)")
    return score, why


def build(diff: str, g: Graph, repo: Path | None,
          ov: dict[str, list[str]], cap: int) -> dict:
    keep, dropped = [], []
    for h in parse_hunks(diff):
        why = ("skip-glob" if hit(h.path, ov["skip"]) else
               "whitespace-only" if h.is_whitespace_only else None)
        (dropped if why else keep).append(
            {"path": h.path, "line": h.start, "reason": why} if why else h)

    by_pkg: dict[str, list[Hunk]] = defaultdict(list)
    for h in keep:
        by_pkg["/".join(h.path.split("/")[:-1]) or "."].append(h)

    sens_ids = g.sensitive_ids(ov["sensitive"])    # computed once, not per bundle

    out = []
    for pkg, hs in sorted(by_pkg.items()):
        hs.sort(key=lambda h: (h.path, h.start))       # do not interleave files
        for i in range(0, len(hs), cap):
            chunk = hs[i:i + cap]
            syms, sym_ids, ents, seen, unmapped = [], [], [], set(), False
            for h in chunk:
                ents += [e for e in entries(repo, h) if e not in ents]
                nodes, prec = g.nodes_for(h)
                if prec == "none":
                    unmapped = True
                    continue
                for n in nodes:
                    lbl = str(n.get("label", ""))
                    if not lbl.startswith(".") or lbl in seen:   # methods: §9.1
                        continue
                    seen.add(lbl)
                    sym_ids.append(n["id"])
                    syms.append({
                        "symbol": lbl, "line": line(n), "match_precision": prec,
                        # §9.2: zero callers is UNKNOWN, never "isolated helper"
                        "callers": self_fan if (self_fan := g.fan_in[n["id"]]) else UNKNOWN,
                        "callees": [g.by_id[c]["label"] for c in g.callees[n["id"]][:8]
                                    if c in g.by_id],
                    })
            sensitive = any(hit(h.path, ov["sensitive"]) for h in chunk)
            # only worth asking when not already sensitive -- that path is
            # already top-ranked (§8); this is for the bundle that is ONE
            # directed call away from one that is (§9.3, fixed)
            hops = (None if sensitive else
                   g.hops_to_sensitive(sym_ids, sens_ids, ov["tests"]))
            b = {
                "id": f"b{len(out):03d}",
                "package": pkg,
                "sensitive": sensitive,
                "facts": {
                    "entry_point_annotations": ents,
                    "symbols": syms,
                    "covering_tests": g.tests_for({h.path for h in chunk}, ov["tests"]),
                    "in_graph": not unmapped,
                    "path_to_sensitive_hops": hops,
                },
                "hunks": [{"path": h.path, "start": h.start, "end": h.end,
                           "text": h.text()} for h in chunk],
            }
            b["risk_hint"], b["risk_hint_basis"] = rank(b)
            out.append(b)

    out.sort(key=lambda b: -b["risk_hint"])
    return {"bundles": out, "dropped": dropped, "stats": {
        "hunks_in_diff": len(keep) + len(dropped),
        "hunks_dropped_free": len(dropped),
        "bundles": len(out),
        "sensitive_bundles": sum(b["sensitive"] for b in out),
        "unmapped_bundles": sum(not b["facts"]["in_graph"] for b in out)}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diff", default="-")
    ap.add_argument("--graph", type=Path)
    ap.add_argument("--repo", type=Path, help="working tree, for the annotation scan")
    ap.add_argument("--overrides", type=Path, default=Path("overrides.toml"))
    # one agent per bundle, each paying a cold cache write (24% of subagent
    # tokens, measured) however little it reviews -- so bundle coarsely
    ap.add_argument("--max-bundle", type=int, default=12)
    ap.add_argument("--no-system1", action="store_true",
                    help="skip tier 1 triage (on by default)")
    ap.add_argument("--gate", action="store_true",
                    help="let tier 1 routing reorder the queue")
    ap.add_argument("--run-linters", action="store_true",
                    help="tier 0: shell out to [[linters]] in overrides.toml "
                         "(opt-in -- CI usually already runs these)")
    a = ap.parse_args()

    if not (a.graph and a.graph.exists()):
        print(f"no graph at {a.graph} -- run `graphify update <repo>` first", file=sys.stderr)
        return 2
    if a.gate and a.no_system1:
        ap.error("--gate needs tier 1; drop --no-system1")

    diff = sys.stdin.read() if a.diff == "-" else Path(a.diff).read_text(
        encoding="utf-8", errors="replace")
    graph_raw = json.loads(a.graph.read_text(encoding="utf-8"))
    fresh = graph_freshness(graph_raw, a.repo)
    if fresh["stale"]:
        print(f"WARNING: graph.json was built at {fresh['built_at_commit'][:12]} but "
              f"HEAD is {fresh['head_commit'][:12]} -- facts may be stale "
              f"(run `graphify update .`)", file=sys.stderr)
    res = build(diff, Graph(graph_raw),
                a.repo, load_overrides(a.overrides), a.max_bundle)
    res["stats"]["graph_freshness"] = fresh

    if not a.no_system1:
        import system1                       # late: review.py works offline
        # A missing key is MISCONFIGURATION -> fail loudly -- but only when
        # opting into the hosted Jev vendor. The default (self-hosted Laya)
        # needs no key at all; an unreachable default is an OUTAGE, not a
        # misconfiguration -> §8 says degrade and escalate, which
        # triage()/route() already do.
        url, _ = system1.env_source("SYSTEM1_URL")
        if system1.requires_key(url or system1.DEFAULT_URL) and not system1.env("TYPESAFE_API_KEY"):
            print("TYPESAFE_API_KEY not found in environment, .env, or the "
                  "Windows registry -- required for the hosted Jev endpoint; "
                  "unset SYSTEM1_URL to use the self-hosted default instead, "
                  "or pass --no-system1 to skip tier 1 entirely", file=sys.stderr)
            return 2
        for b in res["bundles"]:
            b["triage"] = system1.triage(b)
            b["route"] = system1.route(b["triage"], b["sensitive"])
        u = [b["triage"].get("usage") or {} for b in res["bundles"]]
        tok_in = sum(x.get("input_tokens", 0) for x in u)
        tok_out = sum(x.get("output_tokens", 0) for x in u)
        res["stats"]["system1_usage"] = {
            "calls": len(u), "input_tokens": tok_in, "output_tokens": tok_out,
            # $0.042/1M is the published Jev rate (unverified, §4.1)
            "est_usd": round((tok_in + tok_out) * 0.042 / 1e6, 6),
            "model": next((b["triage"].get("model") for b in res["bundles"]
                           if b["triage"].get("model")), None)}
        res["stats"]["system1_ok"] = \
            f"{sum(b['triage']['ok'] for b in res['bundles'])}/{len(res['bundles'])}"
        if a.gate:
            order = {"human+top": 0, "full": 1, "light": 2}
            res["bundles"].sort(key=lambda b: (order.get(b["route"], 1), -b["risk_hint"]))

    if a.run_linters:
        touched = {h["path"] for b in res["bundles"] for h in b["hunks"]}
        res["lint_findings"] = run_linters(a.repo, touched, load_linters(a.overrides))

    json.dump(res, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
