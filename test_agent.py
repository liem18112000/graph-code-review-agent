#!/usr/bin/env python3
"""Self-check for the merge-gate logic in agent.py (no framework, no fixtures).

    python test_agent.py
"""
from __future__ import annotations

import asyncio
import json

import agent


def test_no_verdict_still_blocks():
    findings = [{"severity": "blocker", "scope": "introduced"},
                {"severity": "blocker", "scope": "pre_existing"}]
    for f in findings:
        f["verdict"] = agent.FALLBACK.get(f.get("severity"), "fix")
        f["verdict_by"] = "fallback:--no-verdict"
        if f.get("scope") == "pre_existing" and f["verdict"] == "block":
            f["verdict"] = "note"
    assert findings[0]["verdict"] == "block"       # the bug: this used to stay unset -> 0 blockers
    assert findings[1]["verdict"] == "note"         # pre-existing clamp still holds


def test_adjudicate_rejects_unexpected_choice(monkeypatch):
    import system1
    monkeypatch.setattr(system1, "verdict",
                        lambda f, sensitive=False: {"ok": True, "confidence": 0.95,
                                                     "verdict": "Block", "usage": {}})
    findings = [{"severity": "blocker", "scope": "introduced", "bundle": "b0"}]
    agent.adjudicate(findings, [{"id": "b0", "sensitive": False}])
    assert findings[0]["verdict"] == "block"        # falls back to severity mapping, not "Block"
    assert findings[0]["verdict_by"].startswith("fallback:")


def test_hints_for_never_leaks_route_or_confidence_threshold():
    assert agent.hints_for({}) == []                          # no triage at all
    assert agent.hints_for({"triage": {"ok": False}}) == []    # tier 1 was down
    h = agent.hints_for({"triage": {"ok": True, "risk": "high", "confidence": 0.81,
                                    "security_concern": "authz", "route": "human+top"}})
    assert h == ["possible risk level: high (system-1 confidence 0.81)",
                "possible authz security surface (system-1 confidence 0.81)"]
    assert not any("human+top" in s or "route" in s for s in h)   # route never crosses over
    assert agent.hints_for({"triage": {"ok": True, "risk": "low", "confidence": 0.95,
                                       "security_concern": None}}) == \
          ["possible risk level: low (system-1 confidence 0.95)"]


def test_review_sends_the_payload_via_stdin_not_argv(monkeypatch):
    """The bug: a large bundle as a CLI argument hit Windows' command-line
    length limit (WinError 206) on real runs. The fix is stdin, which has no
    such ceiling -- so the huge bundle must never appear in argv, and must
    reach communicate() as `input`."""
    big_bundle = {"id": "b0", "package": "p", "sensitive": False,
                  "facts": {"symbols": [{"symbol": f".m{i}()"} for i in range(2000)]},
                  "hunks": []}
    seen = {}

    class FakeProc:
        returncode = 0
        async def communicate(self, input=None):
            seen["input"] = input
            return (json.dumps({"result": "[]", "usage": {},
                               "modelUsage": {"opus": {}}}).encode(), b"")
        def kill(self):
            pass

    async def fake_exec(*args, **kwargs):
        seen["argv"] = args
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(agent.review(big_bundle, cwd=".", timeout=5, cheap="opus", full="opus"))

    argv_text = " ".join(str(a) for a in seen["argv"])
    assert ".m1999()" not in argv_text          # the bundle never touches argv
    assert b".m1999()" in seen["input"]         # it goes to stdin instead


def test_sensitive_and_human_top_get_the_deep_search_agent(monkeypatch):
    """graph-reviewer has no search tool, by design, for the common case.
    Sensitive and human+top bundles get graph-reviewer-deep instead (Grep +
    Glob granted via its own frontmatter, not a CLI flag -- --allowedTools
    does not widen an agent's declared tools, verified live before building
    this). Never the model flag's job to decide this; dispatch is by route
    and sensitivity alone."""
    class FakeProc:
        returncode = 0
        async def communicate(self, input=None):
            return (json.dumps({"result": "[]", "usage": {}, "modelUsage": {}}).encode(), b"")
        def kill(self):
            pass

    seen_agents = []

    async def fake_exec(*args, **kwargs):
        seen_agents.append(args[args.index("--agent") + 1])
        return FakeProc()

    import asyncio as _asyncio
    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)

    cases = [({"route": "human+top", "sensitive": False}, agent.AGENT_DEEP),
            ({"route": "full", "sensitive": True}, agent.AGENT_DEEP),
            ({"route": "full", "sensitive": False}, agent.AGENT),
            ({"route": "light", "sensitive": False}, agent.AGENT)]
    for extra, expected in cases:
        bundle = {"id": "b0", "package": "p", "facts": {}, "hunks": [], **extra}
        asyncio.run(agent.review(bundle, cwd=".", timeout=5, cheap="opus", full="opus"))
        assert seen_agents[-1] == expected, (extra, seen_agents[-1])


if __name__ == "__main__":
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)
    test_no_verdict_still_blocks()
    test_adjudicate_rejects_unexpected_choice(_MP())
    test_hints_for_never_leaks_route_or_confidence_threshold()
    test_review_sends_the_payload_via_stdin_not_argv(_MP())
    test_sensitive_and_human_top_get_the_deep_search_agent(_MP())
    print("ok")
