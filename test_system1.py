#!/usr/bin/env python3
"""Self-check for the confidence-field bug in system1.py (no framework).

    python test_system1.py
"""
from __future__ import annotations

import system1


def test_confidence_prefers_answer_confidence_over_confidence():
    """The bug: Laya's response carries two confidence-shaped fields that
    disagree. `answer_confidence` == probabilities[chosen] (what every 0.70
    gate in this codebase means); `confidence` is a separate field that
    collapses toward 0 on anything but a near-certain top choice -- measured
    live: 0.0464 vs answer_confidence 0.4475 on one real triage call."""
    both = {"choice": "block", "confidence": 0.1885, "answer_confidence": 0.6415,
            "probabilities": {"block": 0.6415, "fix": 0.1381, "note": 0.2203}}
    assert system1._confidence(both) == 0.6415

    only_confidence = {"choice": "none", "confidence": 0.92}   # a Jev-shaped reply, say
    assert system1._confidence(only_confidence) == 0.92

    empty = {}
    assert system1._confidence(empty) is None


def test_confidence_falls_back_on_explicit_null_not_just_missing_key():
    """dict.get(k, default) only applies `default` when the key is ABSENT --
    an explicit `"answer_confidence": null` in the JSON still returns the
    stored None, not `confidence`. Found by graph-reviewer dogfooding this
    exact fix."""
    explicit_null = {"answer_confidence": None, "confidence": 0.8}
    assert system1._confidence(explicit_null) == 0.8


def test_confidence_zero_is_not_treated_as_missing():
    """0.0 is a real, legitimate confidence -- `or` would wrongly fall
    through to `confidence` here."""
    zero = {"answer_confidence": 0.0, "confidence": 0.9}
    assert system1._confidence(zero) == 0.0


def test_triage_and_verdict_both_route_through__confidence(monkeypatch):
    """Integration-level, not just the helper in isolation: proves triage()
    and verdict() actually surface answer_confidence, not confidence, end to
    end -- found missing by graph-reviewer dogfooding this exact fix."""
    fake_answer = {"choice": "medium", "confidence": 0.05, "answer_confidence": 0.70,
                   "probabilities": {"low": 0.15, "medium": 0.70, "high": 0.15}}
    monkeypatch.setattr(system1, "ask",
                        lambda state, questions=None, timeout=20: {
                            "model": "laya-rl-agent",
                            "answers": {system1.Q_RISK: fake_answer, system1.Q_SEC:
                                       {"choice": "none", "confidence": 0.9, "answer_confidence": 0.98}},
                            "usage": {}})
    t = system1.triage({"facts": {"symbols": [], "entry_point_annotations": [],
                                  "covering_tests": [], "in_graph": True},
                        "hunks": [], "package": "a", "sensitive": False})
    assert t["confidence"] == 0.70   # not 0.05

    monkeypatch.setattr(system1, "ask",
                        lambda rec, questions=None, timeout=20: {
                            "answers": {system1.Q_VERDICT: {"choice": "fix", "confidence": 0.05,
                                                            "answer_confidence": 0.70}},
                            "usage": {}})
    v = system1.verdict({"severity": "nitpick", "scope": "introduced"})
    assert v["confidence"] == 0.70   # not 0.05


if __name__ == "__main__":
    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)
    test_confidence_prefers_answer_confidence_over_confidence()
    test_confidence_falls_back_on_explicit_null_not_just_missing_key()
    test_confidence_zero_is_not_treated_as_missing()
    test_triage_and_verdict_both_route_through__confidence(_MP())
    print("ok")
