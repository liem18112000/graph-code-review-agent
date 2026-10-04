#!/usr/bin/env python3
"""Tier 1 -- the swappable system-1 slot (§4.1).

Sends a COMPACT STRUCTURAL RECORD, never the diff: names travel, source does
not, and `features` enforces that at runtime. Both questions use `choice`, the
one primitive correct on both Jev and Laya (Laya's `noul` follows its option
labels, #156).

**Self-hosted Laya is the default -- no key needed, works out of the box.**
Point SYSTEM1_URL at JEV_HOSTED_URL (or anywhere else) to opt into a vendor
that needs TYPESAFE_API_KEY instead. If the default endpoint isn't actually
running, that is an OUTAGE, not a misconfiguration (§8): triage() degrades
every bundle to `full` rather than failing the run.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://localhost:8000/v1/systemone"       # self-hosted laya-serve
JEV_HOSTED_URL = "https://api.typesafe.ai/v1/systemone"  # opt-in; needs a key
DEFAULT_MODEL = "laya"
ENV_FILE = Path(__file__).with_name(".env")


def requires_key(url: str) -> bool:
    """Only the hosted Jev vendor needs TYPESAFE_API_KEY. Self-hosted -- the
    default, or any other override -- needs nothing: an outage there just
    degrades to a full review (route()), it never blocks the run."""
    return url == JEV_HOSTED_URL


def _registry(name: str) -> str:
    """Where `setx` writes. A process started before it ran never sees the
    value -- its environment block was copied at spawn."""
    if os.name != "nt":
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return ""


def env_source(name: str) -> tuple[str, str]:
    """(value, origin). Env, then .env beside this file, then the registry --
    an export survives none of CI, a git hook, or another tool's shell.
    Precedence lives here only; a caller re-deriving it will label it wrong."""
    if v := os.environ.get(name):
        return v, "environment"
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name:
                return v.strip().strip("'\""), ".env"
    if v := _registry(name):
        return v, "registry (set, but not inherited -- reopen the shell)"
    return "", ""


def env(name: str) -> str:
    return env_source(name)[0]

# Only what we cannot already compute. `tests_missing` and `needs_deep_review`
# were dropped: covering_tests is a deterministic tier-0 fact, and deep-review
# is just risk != low. Never pay a model to answer what you already know.
#
# Instructions carry explicit disambiguation because routing gates on
# CONFIDENCE (§7): a vague instruction returns a low-confidence answer, which
# escalates every bundle and silently cancels the tiering cost lever.
Q_RISK, Q_SEC = "Which risk level?", "Which security surface?"

QUESTIONS = {
    Q_RISK: {
        "type": "choice",
        "instructions": (
            "Judge risk from the supplied structural facts only; you cannot see the "
            "source. Treat max_callers 'unknown' as UNMEASURED, never as zero: the "
            "graph records only call edges it could resolve. entry_point, not "
            "unknown_callers, is the authority on whether the runtime invokes this "
            "code -- when entry_point is 'none', unmeasured callers are missing data "
            "and not by themselves a danger signal. covering_tests counts test files "
            "referencing these files; a non-zero count means the behaviour is "
            "exercised whoever calls it. Report high confidence when one criterion "
            "plainly fits, and do not lower it merely because some callers are "
            "unmeasured -- that is the normal state of a framework-wired codebase."),
        "criteria": {
            "low": "No entry point, not a sensitive path, and at least one covering test. Unmeasured callers are expected here and do not raise the level.",
            "medium": "No entry point and not sensitive, but no covering test, or wide measured fan-in",
            "high": "An entry point, a sensitive path, or wide measured fan-in with no covering tests"},
    },
    Q_SEC: {
        "type": "choice",
        "instructions": (
            "Decide from package, symbol and file names plus entry_point. Judge only "
            "the surface these names imply; do not infer a vulnerability you cannot "
            "see. sensitive_path 'yes' means the path matched an auth, migration or "
            "payment glob and already forces human review, so it is not by itself "
            "evidence of which surface applies."),
        "criteria": {
            "none": "Names and entry points imply no security surface",
            "authz": "Permission, tenant, role or access-control surface",
            "input": "An entry point means untrusted input reaches this code",
            "secrets": "Credentials, tokens, keys or cryptographic material"},
    },
}


# The LAST decision in the pipeline. System 2 did the reasoning and wrote the
# finding; what happens to it is a categorical call, which is tier 1's job.
# Keeping it here means the gate cannot drift with reviewer phrasing.
Q_VERDICT = "What should happen to this finding?"

VERDICT_Q = {
    Q_VERDICT: {
        "type": "choice",
        "instructions": (
            "A reasoning model already analysed the change and wrote this finding; "
            "you decide only its disposition, from the summary. scope 'pre_existing' "
            "means the defect predates this change and its author cannot act on it. "
            "reviewer_confidence is the reviewer's own probability the finding is "
            "real -- low confidence argues for 'note', never for discarding it. "
            "sensitive_path 'yes' means the file already forces human review, so it "
            "is not by itself a reason to block."),
        "criteria": {
            "block": "Introduced by this change and would crash, corrupt data or break a contract in production",
            "fix": "Introduced by this change and worth fixing, but merging it will not break production",
            "note": "Pre-existing, speculative or low-confidence -- record it, do not gate the merge on it"},
    },
}


def flat(s: object, n: int = 240) -> str:
    """One line, bounded. Findings are prose; the record must stay scannable
    and must not smuggle a diff across in a `failure_scenario`."""
    return " ".join(str(s or "").split())[:n]


def _confidence(answer: dict) -> float | None:
    """Laya's response carries TWO confidence-shaped fields, and they are not
    close: `answer_confidence` == probabilities[chosen] (the intuitive "how
    sure is the picked answer" signal); `confidence` is a separate, opaque
    number that collapses toward 0 on anything but a near-certain top choice
    (measured live: 0.0464 on a 20/45/36 split, 0.1885 on a 64/14/22 split,
    while `answer_confidence` tracked the top probability both times). Every
    gate in this file means the intuitive one. `confidence` is kept as a
    fallback only for a vendor that doesn't expose `answer_confidence`
    (Jev's exact response shape is unverified -- see §4.1). `answer_confidence`
    may be explicitly null rather than absent -- `dict.get(k, default)` would
    return that null, not the default, so the fallback is written to check the
    value, not just key presence. `0.0` is a real, legitimate confidence and
    must not trigger the fallback either, which rules out `or`."""
    ac = answer.get("answer_confidence")
    return ac if ac is not None else answer.get("confidence")


def verdict(f: dict, sensitive: bool = False) -> dict:
    """Tier 1 adjudicates a tier-2 finding. Never raises -- an outage must
    leave the caller free to fall back to the reviewer's own severity."""
    rec = {"severity_claimed": flat(f.get("severity")) or "unknown",
           "scope": flat(f.get("scope")) or "unknown",
           "reviewer_confidence": f.get("confidence", 0),
           "summary": flat(f.get("summary")),
           "failure": flat(f.get("failure_scenario")),
           "sensitive_path": "yes" if sensitive else "no"}
    try:
        raw = ask(_no_source(rec), VERDICT_Q)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "usage": {}, "state": rec}
    a = (raw.get("answers") or {}).get(Q_VERDICT) or {}
    # `state` rides along so a human correction can later become a labelled
    # laya-evals example (feedback.py export-eval) -- without it, a dataset
    # entry would have no `state` to replay the question against.
    return {"ok": True, "verdict": a.get("choice"), "state": rec,
            "confidence": _confidence(a), "usage": raw.get("usage") or {}}


def features(b: dict) -> dict:
    """~70 tokens, no source. Fits Laya's ~320-token budget, keeping the
    vendor swappable."""
    f, hs = b["facts"], b["hunks"]
    known = [s["callers"] for s in f.get("symbols", []) if isinstance(s.get("callers"), int)]
    rec = {
        "change": f"{', '.join(sorted({h['path'].rsplit('/', 1)[-1] for h in hs})[:4])}"
                  f" ({len(hs)} hunks)",
        "package": b["package"],
        "symbols": ", ".join(s["symbol"] for s in f.get("symbols", [])[:8]) or "none",
        "entry_point": ", ".join(f.get("entry_point_annotations") or []) or "none",
        "max_callers": str(max(known)) if known else "unknown",
        "unknown_callers": sum(1 for s in f.get("symbols", []) if s.get("callers") == "unknown"),
        "covering_tests": len(f.get("covering_tests") or []),
        "sensitive_path": "yes" if b.get("sensitive") else "no",
        "found_in_graph": "yes" if f.get("in_graph") else "no",
    }
    return _no_source(rec)


def _no_source(rec: dict) -> dict:
    """Guard, not a test: source must never reach an external vendor. Every
    field is a single-line name, count or flattened sentence, so a newline or
    a hunk header means diff text got in."""
    if "@@" in (blob := json.dumps(rec)) or "\\n" in blob:   # json escapes \n
        raise ValueError("source code leaked into the system-1 record")
    return rec


def ask(state: dict, questions: dict | None = None, timeout: int = 20) -> dict:
    url = env("SYSTEM1_URL") or DEFAULT_URL
    headers = {"Content-Type": "application/json"}
    if key := env("TYPESAFE_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    # `model` is required by both vendors' APIs. Default is DEFAULT_MODEL
    # ("laya", matching DEFAULT_URL); the hosted Jev vendor wants "jev-latest"
    # or "jev-preview" -- set SYSTEM1_MODEL when overriding SYSTEM1_URL to it.
    req = urllib.request.Request(
        url, json.dumps({"model": env("SYSTEM1_MODEL") or DEFAULT_MODEL,
                         "state": state,
                         "questions": questions or QUESTIONS}).encode(),
        headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def triage(bundle: dict, **kw) -> dict:
    """Never raises: a tier-1 outage must not stop a review."""
    state = features(bundle)
    try:
        raw = ask(state, **kw)
    except Exception as e:                      # transport, HTTP, or bad JSON
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "state": state,
                "usage": {}}
    answers = raw.get("answers") or {}
    got = lambda q: (answers.get(q) or {}) if isinstance(answers.get(q), dict) else {}
    sec = got(Q_SEC).get("choice")
    return {"ok": True,
            "risk": got(Q_RISK).get("choice"),
            # gate on answer_confidence, not confidence (see _confidence());
            # act_probability is a third, separate field and anti-correlated on Laya
            "confidence": _confidence(got(Q_RISK)),
            "security_concern": None if sec in ("none", None) else sec,
            "usage": raw.get("usage") or {},     # input_tokens / output_tokens
            "model": raw.get("model"),
            "state": state}


def route(t: dict, sensitive: bool) -> str:
    """§7 routing. Every absence of signal escalates -- unknown is not safe.

    0.70 / 0.95 were never tuned against a measured distribution; they are
    the obvious "fairly sure" / "very sure" points on a 0-1 scale. Before the
    `_confidence()` fix they were being compared against a field
    (`confidence`) that collapsed toward 0 on anything but a near-certain
    answer, so these thresholds were effectively much stricter than they
    read. Reading `answer_confidence` instead raised measured values by
    roughly 5-10x on the same real calls (e.g. 0.19 -> 0.64). The thresholds
    are unchanged here because they were always meant to gate on a sane
    confidence signal, which this is now closer to -- but this is a real
    behaviour change in production (more bundles will clear `light`/pass the
    verdict gate) and is worth watching, not assumed correct from reasoning
    alone."""
    conf = t.get("confidence")
    if sensitive:
        return "human+top"
    if not t.get("ok"):
        return "full"                       # outage must not downgrade
    if not isinstance(conf, (int, float)) or conf < 0.70:
        return "human+top"
    if t.get("security_concern") or t.get("risk") == "high":
        return "human+top"
    return "light" if t.get("risk") == "low" and conf >= 0.95 else "full"



if __name__ == "__main__":                  # one live call, to verify the contract
    print(json.dumps(ask({"change": "Foo.java (1 hunks)", "package": "a",
                          "symbols": ".put()", "entry_point": "none",
                          "max_callers": "3", "unknown_callers": 0,
                          "covering_tests": 1, "sensitive_path": "no",
                          "found_in_graph": "yes"}), indent=2)[:2000])
