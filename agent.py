#!/usr/bin/env python3
"""bundles.json -> findings.json. Exits 1 on blockers.

    python agent.py --bundles bundles.json > findings.json

Drives `claude -p`, so system 2 runs on the SUBSCRIPTION -- no ANTHROPIC_API_KEY
(§4.1). One call per bundle, not an agent loop: review.py already gathered the
facts (§1). Prompt and tools come from the graph-reviewer agent definition, so
this and the interactive skill review identically (§10).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
import shutil
import sys
from pathlib import Path

AGENT = "graph-reviewer"
# Search tools, structurally granted (a separate agent definition, not a CLI
# flag -- --allowedTools does not widen an agent's own `tools:` frontmatter;
# verified live before building this). Dispatched only for sensitive/human+top
# bundles: real comparisons measured graph-reviewer missing cross-file
# consequences (an exposed route documented only in an nginx config, a shell
# script's health-check gating the whole bootstrap) that a search would catch,
# and this is the one tier where that extra cost is already being paid.
AGENT_DEEP = "graph-reviewer-deep"

# Routes that get the cheap tier (§4's model-tiering cost lever).
CHEAP_ROUTES = {"light"}
# `human+top` alone always gets the top model, matching the documented route
# table (§4): it is the highest-risk tier and needs a human regardless, so
# quality there is not a cost trade-off. `full` used to be hardcoded to
# "opus" too, with no flag -- and since Laya's routing confidence rarely
# clears the 0.70/0.95 floors (§4.1), `full` is where most bundles actually
# land, so that hardcode was most of the real spend in every measured run.
TOP_ROUTES = {"human+top"}
# Bundles dispatched to AGENT_DEEP instead of AGENT -- route OR sensitive,
# not just route, because a sensitive bundle that hasn't yet been triaged
# (tier 1 down, or --no-system1) still deserves the deeper reviewer.
DEEP_ROUTES = TOP_ROUTES

KEYS = ("id", "package", "sensitive", "facts", "hunks")


def hints_for(bundle: dict) -> list[str]:
    """Tier 1's triage, reframed as claims to verify -- never as conclusions
    system 2 inherits. Only `risk` and `security_concern` cross over (never
    `route`, `confidence` thresholds, or anything that could anchor rather
    than prompt verification); the agent prompt requires confirming or
    refuting each explicitly. Empty when tier 1 was down or skipped
    (--no-system1), so a missing hints key is not itself a signal."""
    t = bundle.get("triage") or {}
    if not t.get("ok"):
        return []
    conf = t.get("confidence")
    cs = f"{conf:.2f}" if isinstance(conf, (int, float)) else "unknown"
    hints = [f"possible risk level: {t['risk']} (system-1 confidence {cs})"] if t.get("risk") else []
    if sec := t.get("security_concern"):
        hints.append(f"possible {sec} security surface (system-1 confidence {cs})")
    return hints


def findings_from(stdout: str) -> list[dict]:
    """Pull the findings array out of the agent's reply.

    --output-format json wraps the reply; the reply itself is the JSON array the
    agent definition asks for, sometimes inside a code fence.
    """
    text = json.loads(stdout).get("result", "")
    if not isinstance(text, str):
        return []
    m = re.search(r"\[.*\]", text, re.S)          # tolerate prose or a fence
    return json.loads(m.group(0)) if m else []


async def review(bundle: dict, cwd: Path, timeout: int, cheap: str, full: str) -> list[dict]:
    route = bundle.get("route")
    model = cheap if route in CHEAP_ROUTES else "opus" if route in TOP_ROUTES else full
    agent = AGENT_DEEP if (route in DEEP_ROUTES or bundle.get("sensitive")) else AGENT
    payload = {k: bundle[k] for k in KEYS}
    if hints := hints_for(bundle):
        payload["hints"] = hints
    # The prompt goes on STDIN, not argv: `claude -p` reads it from stdin when
    # no positional prompt is given. A large bundle as a CLI argument hits the
    # OS command-line length limit (Windows CreateProcess: ~32K; measured
    # crashing real bundles with WinError 206) -- stdin has no such ceiling.
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p",
        "--agent", agent, "--model", model,
        "--output-format", "json",
        "--permission-mode", "dontAsk",           # non-interactive: never block
        cwd=cwd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(json.dumps(payload, indent=2).encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"timed out after {timeout}s")

    # Claude Code reports failures as is_error in the JSON on STDOUT; stderr is
    # usually empty, so reading stderr alone loses the actual message.
    body = json.loads(out.decode() or "{}") if out else {}
    if proc.returncode != 0 or body.get("is_error"):
        msg = str(body.get("result") or err.decode(errors="replace")).strip()
        raise RuntimeError(msg[:200] or f"exit {proc.returncode}")
    u = body.get("usage") or {}
    # `modelUsage` often has more than one key -- Claude Code makes small
    # incidental calls (e.g. a title) on a different, cheaper model. The
    # first key is not reliably the one that actually reviewed; the one
    # with the most output tokens is the real reviewer call.
    mu = body.get("modelUsage") or {}
    reviewer_model = max(mu, key=lambda k: mu[k].get("outputTokens", 0), default=model)
    usage = {"model": reviewer_model,
             "input_tokens": u.get("input_tokens", 0),
             "output_tokens": u.get("output_tokens", 0),
             "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
             "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
             # reported even on a subscription: NOTIONAL API-equivalent cost,
             # not a bill. Useful only to compare arms in §10.
             "notional_usd": body.get("total_cost_usd", 0.0),
             "duration_ms": body.get("duration_ms", 0)}
    return ([f | {"bundle": bundle["id"]} for f in findings_from(out.decode())],
            usage)


async def run(bundles: list[dict], cwd: Path, concurrency: int,
              timeout: int, cheap: str, full: str) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)

    async def one(b):
        async with sem:
            try:
                return await review(b, cwd, timeout, cheap, full)
            except Exception as e:                # one bundle must not sink the run
                print(f"{b['id']}: {type(e).__name__}: {e}", file=sys.stderr)
                return [], {}

    pairs = await asyncio.gather(*map(one, bundles))
    return ([f for g, _ in pairs for f in g], [u for _, u in pairs if u])


# Reviewer severity -> verdict, used only when tier 1 is down or hedging.
# Degrade, never downgrade: an unavailable adjudicator keeps system 2's call.
#
# Measured, so do not "fix" this by lowering the bar: tier 1 is confident
# (0.82-1.00) on `note`, which follows from the supplied `scope` field, and
# unconfident (0.27-0.72) on block-vs-fix, which needs production impact it
# cannot see. Three framings were tried, including a block/pass binary that
# was worse still. The split is real, so roughly a third of findings land
# here -- that is the design working, not a tuning failure.
FALLBACK = {"blocker": "block", "should_fix": "fix", "nitpick": "note"}

# review.py's optional --run-linters output (§8 "Deterministic stays
# deterministic"): already a fact, not a claim, so it skips both models --
# no system-2 review, no tier-1 verdict. Severity vocabulary is the linter's
# own (error/warning/info), not the reviewer's (blocker/should_fix/nitpick).
LINT_VERDICT = {"error": "fix", "warning": "note", "info": "note"}


def lint_findings_from(doc: dict) -> list[dict]:
    out = []
    for lf in doc.get("lint_findings") or []:
        lf = dict(lf, confidence=1.0, verdict=LINT_VERDICT.get(lf.get("severity"), "note"),
                  verdict_by="lint")
        out.append(lf)
    return out


def adjudicate(findings: list[dict], bundles: list[dict]) -> list[dict]:
    """Tier 1 decides each finding's disposition; this just applies it.

    The LLM reasons, system 1 chooses, and the exit code follows the choice --
    so the merge gate stops drifting with how a reviewer worded `severity`.
    """
    import system1
    sens = {b["id"]: b.get("sensitive", False) for b in bundles}
    out = []
    for f in findings:
        v = system1.verdict(f, sens.get(f.get("bundle"), False))
        ok = (v.get("ok") and (v.get("confidence") or 0) >= 0.70
              and v.get("verdict") in ("block", "fix", "note"))
        f["verdict"] = v["verdict"] if ok else FALLBACK.get(f.get("severity"), "fix")
        f["verdict_by"] = "system1" if ok else "fallback:" + str(v.get("error", "low-confidence"))[:60]
        # kept so a human correction can later become a labelled laya-evals
        # example (feedback.py export-eval) -- the flattened record IS the
        # `state` that question was actually asked against
        if state := v.get("state"):
            f["tier1_state"] = state
        # deterministic clamp, not a model's call: a defect this diff did not
        # introduce cannot gate its merge however bad it is
        if f.get("scope") == "pre_existing" and f["verdict"] == "block":
            f["verdict"] = "note"
        if u := v.get("usage"):
            out.append(u)
    return out


def accounting(stats: dict, usage: list[dict], v_usage: list[dict] = ()) -> dict:
    """Token spend per system. Not comparable as money: system 1 is metered
    API tokens; system 2 is the subscription, where notional_usd is an
    API-equivalent figure, not a bill."""
    tot = lambda k: sum(u[k] for u in usage)
    t1 = dict(stats.get("system1_usage") or {"calls": 0})
    # two tier-1 call sites now: triage before the review, adjudication after
    t1["verdict_calls"] = len(v_usage)
    t1["verdict_tokens"] = sum(u.get("input_tokens", 0) + u.get("output_tokens", 0)
                               for u in v_usage)
    return {"system1_jev": t1,
            "system2_claude": {
                "calls": len(usage),
                "models": Counter(u["model"] for u in usage),
                "input_tokens": tot("input_tokens"),
                "output_tokens": tot("output_tokens"),
                "cache_read_tokens": tot("cache_read_input_tokens"),
                "cache_write_tokens": tot("cache_creation_input_tokens"),
                "notional_usd": round(tot("notional_usd"), 4),
                "billed": "subscription -- notional_usd is not a bill",
                "wall_ms": max((u["duration_ms"] for u in usage), default=0)}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundles", default="-", help="review.py output; - for stdin")
    ap.add_argument("--repo", type=Path, default=Path("."),
                    help="working dir for the agent's Read tool")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=600, help="seconds per bundle")
    ap.add_argument("--no-verdict", action="store_true",
                    help="skip tier-1 adjudication; keep the reviewer's severity")
    ap.add_argument("--cheap-model", default="opus",
                    help="model for 'light' bundles (low risk, high tier-1 confidence)")
    ap.add_argument("--model", default="opus",
                    help="model for 'full' bundles -- everything that isn't 'light' or "
                         "'human+top'. This is most bundles while tier-1 confidence is "
                         "low (§4.1), so it is the main cost lever; 'human+top' always "
                         "gets the top model regardless of this flag")
    a = ap.parse_args()

    if not shutil.which("claude"):
        print("claude not on PATH -- this agent runs on the Claude Code "
              "subscription, not an API key", file=sys.stderr)
        return 2

    doc = json.loads(sys.stdin.read() if a.bundles == "-"
                     else Path(a.bundles).read_text("utf-8"))
    bundles = doc["bundles"]
    findings, usage = asyncio.run(run(bundles, a.repo, a.concurrency,
                                      a.timeout, a.cheap_model, a.model))

    rank = {"blocker": 0, "should_fix": 1, "nitpick": 2}
    # pre-existing defects are real but the author of this diff cannot act on
    # them, so they sink below everything introduced regardless of severity
    findings.sort(key=lambda f: (f.get("scope") == "pre_existing",
                                 rank.get(f.get("severity"), 3),
                                 -f.get("confidence", 0)))
    if a.no_verdict:
        v_usage = []
        for f in findings:                    # same fallback adjudicate() uses when tier 1 is down
            f["verdict"] = FALLBACK.get(f.get("severity"), "fix")
            f["verdict_by"] = "fallback:--no-verdict"
            if f.get("scope") == "pre_existing" and f["verdict"] == "block":
                f["verdict"] = "note"
    else:
        v_usage = adjudicate(findings, bundles)

    # lint findings are deterministic and already carry a verdict -- prepend
    # them, don't run them back through sort/adjudicate's reviewer-severity logic
    findings = lint_findings_from(doc) + findings
    json.dump({"findings": findings}, sys.stdout, indent=2)
    print()

    acct = accounting(doc["stats"], usage, v_usage)
    print(json.dumps(acct, indent=2), file=sys.stderr)

    # otherwise silent: light bundles run at full cost and nobody notices
    if (lt := sum(b.get("route") in CHEAP_ROUTES for b in bundles)) and a.cheap_model == "opus":
        print(f"note: {lt} 'light' bundle(s) ran on opus -- model tiering is "
              "OFF; set --cheap-model", file=sys.stderr)

    new = [f for f in findings if f.get("scope") != "pre_existing"]
    blockers = sum(f.get("verdict") == "block" for f in findings)
    sensitive = sum(b.get("sensitive", False) for b in bundles)
    print(f"{len(findings)} findings ({len(findings)-len(new)} pre-existing), "
          f"{blockers} block, {sensitive} sensitive bundle(s) need a human",
          file=sys.stderr)
    return 1 if blockers or sensitive else 0      # non-zero fails CI


if __name__ == "__main__":
    raise SystemExit(main())
