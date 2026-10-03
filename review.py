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


def load_overrides(p: Path | None) -> dict[str, list[str]]:
    # fall back to the copy shipped beside this script: a target repo usually
    # has no overrides.toml of its own, and empty globs silently disable tier 0
    if not (p and p.exists()):
        p = DEFAULT_OVERRIDES if DEFAULT_OVERRIDES.exists() else None
    raw = tomllib.loads(p.read_text(encoding="utf-8")) if p and p.exists() else {}
    return {k: raw.get(k, {}).get("globs", []) for k in KINDS}


def hit(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


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

    out = []
    for pkg, hs in sorted(by_pkg.items()):
        hs.sort(key=lambda h: (h.path, h.start))       # do not interleave files
        for i in range(0, len(hs), cap):
            chunk = hs[i:i + cap]
            syms, ents, seen, unmapped = [], [], set(), False
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
                    syms.append({
                        "symbol": lbl, "line": line(n), "match_precision": prec,
                        # §9.2: zero callers is UNKNOWN, never "isolated helper"
                        "callers": self_fan if (self_fan := g.fan_in[n["id"]]) else UNKNOWN,
                        "callees": [g.by_id[c]["label"] for c in g.callees[n["id"]][:8]
                                    if c in g.by_id],
                    })
            b = {
                "id": f"b{len(out):03d}",
                "package": pkg,
                "sensitive": any(hit(h.path, ov["sensitive"]) for h in chunk),
                "facts": {
                    "entry_point_annotations": ents,
                    "symbols": syms,
                    "covering_tests": g.tests_for({h.path for h in chunk}, ov["tests"]),
                    "in_graph": not unmapped,
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
    a = ap.parse_args()

    if not (a.graph and a.graph.exists()):
        print(f"no graph at {a.graph} -- run `graphify update <repo>` first", file=sys.stderr)
        return 2
    if a.gate and a.no_system1:
        ap.error("--gate needs tier 1; drop --no-system1")

    diff = sys.stdin.read() if a.diff == "-" else Path(a.diff).read_text(
        encoding="utf-8", errors="replace")
    res = build(diff, Graph(json.loads(a.graph.read_text(encoding="utf-8"))),
                a.repo, load_overrides(a.overrides), a.max_bundle)

    if not a.no_system1:
        import system1                       # late: review.py works offline
        # A missing key is MISCONFIGURATION -> fail loudly -- but only for the
        # hosted Jev endpoint. A self-hosted SYSTEM1_URL (e.g. laya-serve) is
        # the whole point of §4.1's swap: no key needed. A key whose endpoint
        # is down is an OUTAGE -> §8 says degrade and escalate, which
        # triage()/route() already do.
        url, _ = system1.env_source("SYSTEM1_URL")
        if not (url and url != system1.DEFAULT_URL) and not system1.env("TYPESAFE_API_KEY"):
            print("TYPESAFE_API_KEY not found in environment, .env, or the "
                  "Windows registry -- tier 1 is required; pass --no-system1 "
                  "to skip", file=sys.stderr)
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

    json.dump(res, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
