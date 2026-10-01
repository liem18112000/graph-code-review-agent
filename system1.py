#!/usr/bin/env python3
"""Tier 1 -- the swappable system-1 slot (§4.1). Key in .env or the environment.

Sends a COMPACT STRUCTURAL RECORD, never the diff: names travel, source does
not, and `features` enforces that at runtime. Both questions use `choice`, the
one primitive correct on both Jev and Laya (Laya's `noul` follows its option
labels, #156). Point SYSTEM1_URL at a local laya-serve to swap vendor.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
ENV_FILE = Path(__file__).with_name(".env")


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
    # Guard, not a test: source code must never reach an external vendor.
    # Every field above is a single-line name or count, so a newline or a hunk
    # header means diff text got in.
    blob = json.dumps(rec)
    if "@@" in blob or "\\n" in blob:     # json escapes a real newline to \n
        raise ValueError("source code leaked into the system-1 record")
    return rec


def ask(state: dict, questions: dict | None = None, timeout: int = 20) -> dict:
    url = env("SYSTEM1_URL") or DEFAULT_URL
    headers = {"Content-Type": "application/json"}
    if key := env("TYPESAFE_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    # `model` is required by the real API (422 without it). GET /v1/models
    # lists what this key may use: jev-latest (stable), jev-preview.
    req = urllib.request.Request(
        url, json.dumps({"model": env("SYSTEM1_MODEL") or "jev-latest",
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
            # gate on confidence: act_probability is anti-correlated on Laya
            "confidence": got(Q_RISK).get("confidence"),
            "security_concern": None if sec in ("none", None) else sec,
            "usage": raw.get("usage") or {},     # input_tokens / output_tokens
            "model": raw.get("model"),
            "state": state}


def route(t: dict, sensitive: bool) -> str:
    """§7 routing. Every absence of signal escalates -- unknown is not safe."""
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
