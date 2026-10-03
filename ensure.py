#!/usr/bin/env python3
"""Preflight: verify every dependency the agent needs, on any platform.

    python ensure.py           # fast: presence + versions + imports, no network
    python ensure.py --deep    # also probes the tier-1 endpoint and Claude Code

Exits 0 when every REQUIRED check passes, 1 otherwise. Optional checks only warn.

Deliberately written in Python 3.6-compatible syntax -- no walrus, no `dict | dict`,
no `X | Y` annotations -- because a version check that cannot run on the versions
it rejects reports a SyntaxError instead of the actual problem.
"""
import os
import platform
import shutil
import subprocess
import sys

REQ_PY = (3, 11)          # tomllib landed in 3.11
OK, WARN, FAIL = "OK", "WARN", "FAIL"
WIN = platform.system() == "Windows"
MAC = platform.system() == "Darwin"
rows = []

# Priority is the order a bare machine must satisfy them in.
MUST1, MUST2, REQ, OPT = "must-1", "must-2", "required", "optional"


def pick(win, mac, linux):
    return win if WIN else (mac if MAC else linux)


PY_FIX = pick("winget install Python.Python.3.12",
              "brew install python@3.12",
              "sudo apt install python3.12  (or your distro equivalent)")
CLAUDE_FIX = "npm install -g @anthropic-ai/claude-code   then run `claude` once to log in"
GIT_FIX = pick("winget install Git.Git", "brew install git", "sudo apt install git")
# no invented install command: graphify's distribution is not documented here
GRAPHIFY_FIX = "install graphify per its own docs and put it on PATH"
KEY_FIX = pick('setx TYPESAFE_API_KEY "<key>"   (reopen the shell afterwards)',
               'export TYPESAFE_API_KEY=<key>   (add to ~/.zshrc)',
               'export TYPESAFE_API_KEY=<key>   (add to ~/.bashrc)'
               ) + "   -- or run tier 1 self-hosted: set SYSTEM1_URL instead, no key needed"


def add(name, tier, status, detail, fix=""):
    rows.append((name, tier, status, detail, fix))
    return status


def run(cmd, timeout=25):
    """(returncode, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ss" % timeout
    except Exception as e:                       # noqa: BLE001 - report, never crash
        return 1, "%s: %s" % (type(e).__name__, e)


def check_python():
    v = sys.version_info
    got = "%d.%d.%d" % (v[0], v[1], v[2])
    if v[:2] < REQ_PY:
        return add("python", REQ, FAIL, "%s -- need >= %d.%d" % (got, REQ_PY[0], REQ_PY[1]),
                   PY_FIX)
    try:
        import tomllib                            # noqa: F401
    except ImportError:
        return add("python", REQ, FAIL, got + " but tomllib missing", PY_FIX)
    return add("python", REQ, OK, got + " (tomllib present)")


def check_cmd(name, args, tier, fix, extra=None):
    exe = shutil.which(name)
    if not exe:
        return add(name, tier, FAIL if tier != OPT else WARN, "not on PATH", fix)
    code, out = run([exe] + args)
    if code != 0:
        return add(name, tier, FAIL if tier != OPT else WARN, out.splitlines()[0][:60], fix)
    line = out.splitlines()[0][:60] if out else "present"
    if extra:
        line += " | " + extra(exe)
    return add(name, tier, OK, line)


def check_modules():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    missing = []
    for m in ("diffparse", "review", "system1", "agent", "bench"):
        try:
            __import__(m)
        except Exception as e:                    # noqa: BLE001
            missing.append("%s (%s)" % (m, type(e).__name__))
    if missing:
        return add("modules", REQ, FAIL, ", ".join(missing),
                   "run from the plugin directory, or reinstall the plugin")
    return add("modules", REQ, OK, "5 modules import cleanly")


def check_agent():
    """The reviewer prompt may come from the plugin or from ~/.claude/agents."""
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, "agents", "graph-reviewer.md")
    user = os.path.join(os.path.expanduser("~"), ".claude", "agents",
                        "graph-reviewer.md")
    for path, where in ((local, "plugin"), (user, "user-level")):
        if os.path.isfile(path):
            return add("graph-reviewer agent", REQ, OK, "found (%s)" % where)
    return add("graph-reviewer agent", REQ, FAIL, "not found",
               "claude plugin install graph-code-review-agent")


def check_key():
    """Two supported modes (README "Swapping the classifier"): hosted Jev with
    a key, or a self-hosted SYSTEM1_URL (e.g. laya-serve) with none. Mirrors
    the same gate review.py applies before tier 1 runs -- a key is only a MUST
    against the default hosted endpoint."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import system1
    except Exception:                             # covered by check_modules
        return add("TYPESAFE_API_KEY", MUST1, FAIL, "cannot import system1", "")
    url, _ = system1.env_source("SYSTEM1_URL")
    self_hosted = bool(url) and url != system1.DEFAULT_URL
    key, src = system1.env_source("TYPESAFE_API_KEY")
    if key:
        return add("TYPESAFE_API_KEY", MUST1, OK, "%d chars, from %s" % (len(key), src))
    if self_hosted:
        return add("TYPESAFE_API_KEY", MUST1, OK,
                   "not set -- SYSTEM1_URL overridden to %s, no key needed" % url)
    where = ("environment, .env, or the registry" if WIN
             else "environment or .env")
    return add("TYPESAFE_API_KEY", MUST1, FAIL, "not in " + where, KEY_FIX)


def check_system1_target():
    """Informational only -- shows which tier-1 mode is actually configured,
    so a misread env var is visible before --deep spends a real call on it."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import system1
    url, src = system1.env_source("SYSTEM1_URL")
    if not url:
        return add("tier-1 endpoint", OPT, OK,
                   "default (hosted Jev, %s)" % system1.DEFAULT_URL)
    mode = "default" if url == system1.DEFAULT_URL else "self-hosted override"
    return add("tier-1 endpoint", OPT, OK, "%s -- %s (from %s)" % (url, mode, src))


def check_graph():
    p = os.path.join(os.getcwd(), "graphify-out", "graph.json")
    if not os.path.isfile(p):
        return add("graph.json", OPT, WARN, "none in this directory",
                   "graphify update .   (per repo you want to review)")
    mb = os.path.getsize(p) / 1e6
    return add("graph.json", OPT, OK, "%.1f MB in ./graphify-out" % mb)


def deep_system1():
    import system1
    try:
        r = system1.ask({"change": "preflight", "package": "n/a", "symbols": "none",
                         "entry_point": "none", "max_callers": "unknown",
                         "unknown_callers": 0, "covering_tests": 0,
                         "sensitive_path": "no", "found_in_graph": "no"})
    except Exception as e:                        # noqa: BLE001
        return add("tier-1 endpoint", MUST1, FAIL, "%s: %s" % (type(e).__name__, str(e)[:50]),
                   "check TYPESAFE_API_KEY and SYSTEM1_URL")
    model = r.get("model", "?")
    n = len(r.get("answers") or {})
    return add("tier-1 endpoint", MUST1, OK, "%s answered %d question(s)" % (model, n))


def deep_claude():
    exe = shutil.which("claude")
    if not exe:
        return add("claude auth", MUST2, FAIL, "claude not on PATH", CLAUDE_FIX)
    code, out = run([exe, "-p", "Reply with exactly: ok",
                     "--output-format", "json", "--permission-mode", "dontAsk"], 120)
    if code != 0:
        return add("claude auth", MUST2, FAIL, out.splitlines()[-1][:60] if out else "exit %d" % code,
                   "run `claude` once interactively to authenticate")
    import json
    try:
        body = json.loads(out)
    except ValueError:
        return add("claude auth", MUST2, FAIL, "unparseable response", "")
    if body.get("is_error"):
        # the useful message is in `result` on stdout, not stderr
        return add("claude auth", MUST2, FAIL, str(body.get("result"))[:60],
                   "check `claude` auth and model access")
    models = list((body.get("modelUsage") or {}).keys())
    return add("claude auth", MUST2, OK, "subscription ok" +
               (" | " + models[0] if models else ""))


def main():
    deep = "--deep" in sys.argv
    print("graph-code-review-agent preflight  (%s %s, %s)\n" %
          (platform.system(), platform.release(), platform.machine()))

    check_python()
    check_modules()
    check_key()                                   # must-1
    check_system1_target()
    check_cmd("claude", ["--version"], MUST2, CLAUDE_FIX)
    check_cmd("git", ["--version"], REQ, GIT_FIX)
    check_cmd("graphify", ["--version"], REQ, GRAPHIFY_FIX)
    check_agent()
    check_graph()
    check_cmd("defects4j", ["--help"], OPT,
              "optional: bash setup-defects4j.sh  (only needed for bench.py)")

    if deep:
        print("  ...running deep checks (network + one model call)\n")
        deep_system1()
        deep_claude()

    w = max(len(r[0]) for r in rows)
    order = {MUST1: 0, MUST2: 1, REQ: 2, OPT: 3}
    label = {MUST1: "MUST 1 -- TypeSafe API key",
             MUST2: "MUST 2 -- Claude Code usable",
             REQ: "Required tooling", OPT: "Optional"}
    shown = None
    for name, tier, status, detail, fix in sorted(rows, key=lambda r: order[r[1]]):
        if tier != shown:
            shown = tier
            print("\n" + label[tier])
        mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}[status]
        print("  [%s] %-*s  %s" % (mark, w, name, detail))
        if fix and status != OK:
            print("%s-> %s" % (" " * (w + 13), fix))

    bad = [r for r in rows if r[2] == FAIL]
    warn = [r for r in rows if r[2] == WARN]
    print("\n%d ok, %d warning(s), %d failure(s)" %
          (len(rows) - len(bad) - len(warn), len(warn), len(bad)))
    if bad:
        print("\nNOT READY. Fix in this order:")
        for i, r in enumerate(sorted(bad, key=lambda r: order[r[1]]), 1):
            print("  %d. %-22s %s" % (i, r[0], r[4] or "see above"))
    else:
        print("\nREADY." + ("" if deep else
              "  Run --deep to prove the endpoint and Claude auth for real."))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
