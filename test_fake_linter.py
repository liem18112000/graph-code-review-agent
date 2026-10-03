#!/usr/bin/env python3
"""Fixture for test_review.py's run_linters test -- emits this project's
lint contract ({path, line, severity, message}) for each arg it's given."""
import json
import sys

print(json.dumps([
    {"path": p, "line": 1, "severity": "warning", "message": f"fake lint hit on {p}"}
    for p in sys.argv[1:]
]))
