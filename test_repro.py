#!/usr/bin/env python3
"""Regression tests for three silent-recall bugs: root-level glob misses,
the fail-open merge gate, and indentation changes dropped as whitespace.

    python -m pytest test_repro.py
"""
from __future__ import annotations

import io
import json
import sys

import pytest

import agent
import review
from diffparse import parse_hunks

OV = review.load_overrides(None)          # the shipped overrides.toml
EMPTY_GRAPH = review.Graph({"nodes": [], "links": []})


def _diff(path: str, minus: list[str], plus: list[str]) -> str:
    body = [f"-{l}" for l in minus] + [f"+{l}" for l in plus]
    return (f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(minus)} +1,{len(plus)} @@\n" + "\n".join(body) + "\n")


# --- bug 1: `**/x` globs never match a file at the repo root -----------------

@pytest.mark.parametrize("path", ["package-lock.json", "yarn.lock", "README.md"])
def test_root_level_skip_globs_match(path):
    res = review.build(_diff(path, ["a"], ["b"]), EMPTY_GRAPH, None, OV, 12)
    assert [d["path"] for d in res["dropped"]] == [path]
    assert res["bundles"] == []


@pytest.mark.parametrize("path", ["auth/Login.java", "schema.sql",
                                  "migrations/0001_init.py"])
def test_root_level_sensitive_globs_match(path):
    res = review.build(_diff(path, ["a"], ["b"]), EMPTY_GRAPH, None, OV, 12)
    assert res["bundles"][0]["sensitive"] is True


def test_nested_paths_still_match():
    assert review.hit("src/auth/Login.java", OV["sensitive"])
    assert review.hit("web/package-lock.json", OV["skip"])
    assert not review.hit("src/author/Book.java", OV["sensitive"])


# --- bug 2: a failed bundle review must not let the gate pass ----------------

def _reply(result: str) -> str:
    return json.dumps({"result": result})


def test_findings_from_tolerates_brackets_in_prose():
    text = ('Reviewed bundle [b001] below.\n```json\n'
            '[{"file": "a.py", "line": 3, "severity": "blocker"}]\n```')
    assert agent.findings_from(_reply(text)) == [
        {"file": "a.py", "line": 3, "severity": "blocker"}]


def test_findings_from_accepts_explicit_empty_array():
    assert agent.findings_from(_reply("[]")) == []


def test_findings_from_reply_without_array_is_an_error():
    with pytest.raises(ValueError):
        agent.findings_from(_reply("I could not review this bundle."))


def test_failed_bundle_fails_the_gate(monkeypatch, capsys):
    async def boom(*a, **kw):
        raise RuntimeError("authentication expired")

    doc = {"bundles": [{"id": "b000", "package": "p", "sensitive": False,
                        "facts": {}, "hunks": []}], "stats": {}}
    monkeypatch.setattr(agent, "review", boom)
    monkeypatch.setattr(agent.shutil, "which", lambda _: "claude")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(doc)))
    monkeypatch.setattr(sys, "argv", ["agent.py", "--no-verdict"])

    code = agent.main()

    out = json.loads(capsys.readouterr().out)
    assert code != 0                       # the bug: 0 findings, exit 0, CI green
    assert out["failed_bundles"] == ["b000"]


# --- bug 4: indentation is syntax in Python / YAML ---------------------------

def test_python_dedent_is_not_whitespace_only():
    # moves the call out of the `if` block -- a behaviour change
    h, = parse_hunks(_diff("src/app.py", ["        notify()"], ["    notify()"]))
    assert not h.is_whitespace_only


def test_yaml_reindent_is_not_whitespace_only():
    h, = parse_hunks(_diff("deploy/values.yaml", ["    debug: true"], ["  debug: true"]))
    assert not h.is_whitespace_only


def test_java_reindent_is_still_whitespace_only():
    h, = parse_hunks(_diff("src/Foo.java", ["        call();"], ["    call();"]))
    assert h.is_whitespace_only


def test_python_trailing_whitespace_is_still_whitespace_only():
    h, = parse_hunks(_diff("src/app.py", ["    notify()   "], ["    notify()"]))
    assert h.is_whitespace_only
