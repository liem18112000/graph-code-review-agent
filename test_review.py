#!/usr/bin/env python3
"""Self-check for the graph-freshness and path-to-sensitive logic in
review.py (no framework, no fixtures).

    python test_review.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import review

FAKE_LINTER = Path(__file__).with_name("test_fake_linter.py")


def _graph():
    return review.Graph({
        "nodes": [
            {"id": "a", "source_file": "src/foo.py", "label": ".foo()"},
            {"id": "b", "source_file": "src/bar.py", "label": ".bar()"},
            {"id": "c", "source_file": "src/auth/token.py", "label": ".checkToken()"},
            {"id": "t", "source_file": "src/test/fooTest.py", "label": ".testFoo()"},
        ],
        "links": [
            {"source": "a", "target": "b", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "b", "target": "c", "relation": "calls", "confidence": "EXTRACTED"},
            {"source": "t", "target": "c", "relation": "calls", "confidence": "EXTRACTED"},
        ],
    })


def test_hops_to_sensitive_walks_directed_calls_only():
    g = _graph()
    sens = g.sensitive_ids(["**/auth/**"])
    assert sens == {"c"}
    assert g.hops_to_sensitive(["a"], sens, ["**/test/**"]) == 2   # a -> b -> c
    assert g.hops_to_sensitive(["b"], sens, ["**/test/**"]) == 1   # b -> c
    assert g.hops_to_sensitive(["missing"], sens, ["**/test/**"]) is None
    assert g.hops_to_sensitive([], sens, ["**/test/**"]) is None
    assert g.hops_to_sensitive(["a"], set(), ["**/test/**"]) is None


def test_hops_to_sensitive_respects_max_hops_cap():
    g = _graph()
    sens = g.sensitive_ids(["**/auth/**"])
    assert g.hops_to_sensitive(["a"], sens, ["**/test/**"], max_hops=1) is None
    assert g.hops_to_sensitive(["a"], sens, ["**/test/**"], max_hops=2) == 2


def test_graph_freshness_detects_stale_and_missing_repo():
    fresh = review.graph_freshness({"built_at_commit": "deadbeef"}, None)
    assert fresh == {"checked": False, "built_at_commit": "deadbeef",
                     "head_commit": None, "stale": None}

    no_stamp = review.graph_freshness({}, review.Path("."))
    assert no_stamp["checked"] is False and no_stamp["stale"] is None

    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                          text=True, timeout=10).stdout.strip()
    if head:
        match = review.graph_freshness({"built_at_commit": head}, review.Path("."))
        assert match == {"checked": True, "built_at_commit": head,
                         "head_commit": head, "stale": False}
        stale = review.graph_freshness({"built_at_commit": "not-a-real-sha"}, review.Path("."))
        assert stale["checked"] is True and stale["stale"] is True


def test_run_linters_shells_out_and_respects_glob():
    linters = [{"name": "fake", "glob": "**/*.py",
               "cmd": [sys.executable, str(FAKE_LINTER)]}]
    out = review.run_linters(None, {"pkg/review.py", "pkg/agent.py", "README.md"}, linters)
    assert len(out) == 2                        # README.md doesn't match **/*.py
    assert {o["file"] for o in out} == {"pkg/review.py", "pkg/agent.py"}
    assert all(o["source"] == "lint:fake" and o["category"] == "lint" for o in out)


def test_run_linters_survives_a_broken_command():
    linters = [{"name": "broken", "glob": "**/*.py", "cmd": ["not-a-real-command-xyz"]}]
    assert review.run_linters(None, {"pkg/a.py"}, linters) == []   # never raises


if __name__ == "__main__":
    test_hops_to_sensitive_walks_directed_calls_only()
    test_hops_to_sensitive_respects_max_hops_cap()
    test_graph_freshness_detects_stale_and_missing_repo()
    test_run_linters_shells_out_and_respects_glob()
    test_run_linters_survives_a_broken_command()
    print("ok")
