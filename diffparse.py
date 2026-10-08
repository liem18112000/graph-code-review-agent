#!/usr/bin/env python3
"""Unified-diff parsing. Shared by review.py and bench.py."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$")
# Leading whitespace is syntax here: a dedent moves code out of a block.
INDENT_SIGNIFICANT = (".py", ".pyi", ".pyx", ".yaml", ".yml", ".mk", "Makefile")


def norm(p: str) -> str:
    p = p.replace("\\", "/").strip()
    return p[2:] if p[:2] in ("a/", "b/") else p


@dataclass
class Hunk:
    path: str
    start: int
    body: list[str] = field(default_factory=list)
    context: str = ""

    @property
    def touched(self) -> list[int]:
        """New-file lines changed. A deletion consumes no new line, so it
        anchors where it was -- a removed guard is flagged there."""
        out, ln = [], self.start
        for l in self.body:
            if l.startswith("+"):
                out.append(ln); ln += 1
            elif l.startswith("-"):
                out.append(ln)
            else:
                ln += 1
        return out

    @property
    def end(self) -> int:
        return self.start + max(0, sum(not l.startswith("-") for l in self.body) - 1)

    @property
    def is_whitespace_only(self) -> bool:
        """Collapse whitespace only outside string literals: inside one it is
        content, and a false positive here is a real change never reviewed.
        Indentation is kept verbatim where the language makes it syntax."""
        sig = self.path.endswith(INDENT_SIGNIFICANT)

        def k(l: str) -> str:
            b = l[1:]
            lead = b[:len(b) - len(b.lstrip())] if sig else ""
            return lead + (b.strip() if ('"' in b or "'" in b)
                           else re.sub(r"\s+", " ", b).strip())
        a = [k(l) for l in self.body if l.startswith("+")]
        r = [k(l) for l in self.body if l.startswith("-")]
        return bool(a or r) and a == r

    def text(self) -> str:
        return "\n".join([f"@@ {self.path}:{self.start} @@{self.context}", *self.body])


def parse_hunks(diff: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    path = cur = None
    for l in diff.splitlines():
        if l.startswith("+++ "):
            p = l[4:].split("\t")[0].strip()
            path, cur = (None if p == "/dev/null" else norm(p)), None
        elif l.startswith(("--- ", "diff ", "\\")):
            continue
        elif m := HUNK_RE.match(l):
            cur = Hunk(path, int(m.group(1)), [], m.group(2).rstrip()) if path else None
            if cur:
                hunks.append(cur)
        elif cur is not None and l[:1] in ("+", "-", " ", ""):
            cur.body.append(l)
    return [h for h in hunks if h.body]


