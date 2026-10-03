#!/usr/bin/env python3
"""Minimal MCP server (stdio, JSON-RPC 2.0, line-delimited) exposing this
repo's review capability to other tools. graphify's own MCP mode does not
exist (§9.5: "`graphify install` copies a skill"); this is this project's
own equivalent, independent of graphify ever shipping one. Stdlib only, no
`mcp` SDK dependency -- the stdio transport is simple enough to hand-roll,
and staying dependency-free is this project's whole design.

    python mcp_server.py              # run as a subprocess from an MCP client
    python mcp_server.py selftest     # exercises dispatch directly, no stdio loop
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import rank_queue
import review

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "review_diff",
        "description": "Tier 0 + optional tier 1 (deterministic filters + fast "
                        "triage) over a unified diff, using a prebuilt graphify "
                        "graph. Returns ranked bundles with facts -- the same "
                        "shape as review.py's stdout. Never calls a reasoning "
                        "model; that is a separate step (agent.py).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "unified diff text"},
                "graph_path": {"type": "string", "description": "path to graphify-out/graph.json"},
                "repo_path": {"type": "string", "description": "working tree, for the entry-point annotation scan"},
                "overrides_path": {"type": "string"},
                "no_system1": {"type": "boolean", "default": True,
                               "description": "skip tier-1 triage (needs TYPESAFE_API_KEY or a self-hosted SYSTEM1_URL otherwise)"},
            },
            "required": ["diff", "graph_path"],
        },
    },
    {
        "name": "rank_queue",
        "description": "Rank several branches/PR refs by aggregate risk against "
                        "a common base, reusing review.py's build() per ref -- "
                        "no graphify `prs --triage` needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "graph_path": {"type": "string"},
                "repo_path": {"type": "string"},
                "base": {"type": "string", "default": "origin/main"},
                "refs": {"type": "array", "items": {"type": "string"}},
                "overrides_path": {"type": "string"},
            },
            "required": ["graph_path", "refs"],
        },
    },
]


def call_review_diff(args: dict) -> dict:
    repo = Path(args["repo_path"]) if args.get("repo_path") else None
    overrides = review.load_overrides(Path(args["overrides_path"])
                                      if args.get("overrides_path") else None)
    graph_raw = json.loads(Path(args["graph_path"]).read_text(encoding="utf-8"))
    res = review.build(args["diff"], review.Graph(graph_raw), repo, overrides, 12)
    if not args.get("no_system1", True):
        import system1                           # late: this tool works offline too
        for b in res["bundles"]:
            b["triage"] = system1.triage(b)
            b["route"] = system1.route(b["triage"], b["sensitive"])
    return res


def call_rank_queue(args: dict) -> list[dict]:
    repo = Path(args.get("repo_path") or ".")
    overrides = review.load_overrides(Path(args["overrides_path"])
                                      if args.get("overrides_path") else None)
    graph_raw = json.loads(Path(args["graph_path"]).read_text(encoding="utf-8"))
    return rank_queue.rank_refs(repo, graph_raw, args.get("base", "origin/main"),
                               args["refs"], overrides)


DISPATCH = {"review_diff": call_review_diff, "rank_queue": call_rank_queue}


def handle(msg: dict) -> dict | None:
    """One JSON-RPC request -> one response dict, or None for a notification
    (no `id`) which the stdio transport must not reply to."""
    method, mid = msg.get("method"), msg.get("id")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
            "serverInfo": {"name": "graph-code-review-agent", "version": "0.3.0"}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        fn = DISPATCH.get(params.get("name"))
        if not fn:
            return {"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": f"unknown tool: {params.get('name')}"}}
        try:
            text = json.dumps(fn(params.get("arguments") or {}), indent=2)
            return {"jsonrpc": "2.0", "id": mid,
                   "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as e:          # noqa: BLE001 -- report to the client, never crash the server
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True,
                   "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}]}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
               "error": {"code": -32601, "message": f"unknown method: {method}"}}
    return None


def serve(instream=None, outstream=None) -> None:
    instream = instream or sys.stdin
    outstream = outstream or sys.stdout
    for line in instream:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            outstream.write(json.dumps(resp) + "\n")
            outstream.flush()


def selftest() -> int:
    """No stdio loop -- calls handle() directly against synthetic messages."""
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["protocolVersion"] == PROTOCOL_VERSION
    names = {t["name"] for t in handle({"jsonrpc": "2.0", "id": 2,
                                        "method": "tools/list"})["result"]["tools"]}
    assert names == {"review_diff", "rank_queue"}, names
    notif = handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert notif is None

    graph = Path("graphify-out/graph.json")
    if graph.exists():
        diff = "--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        call = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "review_diff",
            "arguments": {"diff": diff, "graph_path": str(graph), "no_system1": True}}})
        assert not call["result"].get("isError"), call
        assert "bundles" in json.loads(call["result"]["content"][0]["text"])

    unknown = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "nope", "arguments": {}}})
    assert unknown["error"]["code"] == -32601

    broken = handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "review_diff", "arguments": {"diff": "x", "graph_path": "/no/such/file.json"}}})
    assert broken["result"]["isError"] is True

    print("ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        raise SystemExit(selftest())
    serve()
