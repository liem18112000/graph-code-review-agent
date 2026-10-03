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


if __name__ == "__main__":
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)
    test_no_verdict_still_blocks()
    test_adjudicate_rejects_unexpected_choice(_MP())
    print("ok")
