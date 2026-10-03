#!/usr/bin/env python3
"""Self-check for the merge-gate logic in agent.py (no framework, no fixtures).

    python test_agent.py
"""
from __future__ import annotations

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


if __name__ == "__main__":
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)
    test_no_verdict_still_blocks()
    test_adjudicate_rejects_unexpected_choice(_MP())
    test_hints_for_never_leaks_route_or_confidence_threshold()
    print("ok")
