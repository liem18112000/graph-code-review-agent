---
name: graph-review
description: Review a diff using the code graph. Computes structural facts deterministically, triages each change with a fast classifier, then dispatches one graph-reviewer agent per risk-ranked bundle. Use when asked to review a PR, a working diff, or a branch on a repo that has a graphify graph.
---

# Graph-grounded review

Facts first, cheap triage second, reasoning model last. See `docs/architecture.md`
in the plugin for why, `docs/call-flow.md` for what calls what.

`${CLAUDE_PLUGIN_ROOT}` is this plugin's directory — use it for every script path
so the commands work from any repo.

## 1. Check the graph is current

A stale graph degrades every downstream signal **with no error**, so verify before
spending anything:

```bash
git rev-parse HEAD
ls -la graphify-out/graph.json        # must post-date the base commit
```

Out of date → `graphify update .` (local AST parse, no tokens). Missing → also run
`graphify hook install` so it stays current. If `graphify` is absent, say so and
stop — reviewing without facts is the baseline this design exists to beat.

## 2. Build the bundles

```bash
git diff origin/main...HEAD > /tmp/pr.diff
python "${CLAUDE_PLUGIN_ROOT}/review.py" \
    --diff /tmp/pr.diff \
    --graph graphify-out/graph.json \
    --repo . > /tmp/bundles.json
```

This runs tier 0 (free path globs + whitespace detection) **and** tier 1 (Jev
triage) — tier 1 is on by default and needs `TYPESAFE_API_KEY` in the
environment. Exit code 2 means the key is missing; `--no-system1` skips tier 1.

Report `stats`: hunks dropped free, bundles produced, `system1_ok`, and
`system1_usage` (tier-1 token spend).

## 3. Dispatch one agent per bundle

Bundles arrive sorted by `risk_hint`, and each carries a `route` from tier 1.
Send each to the `graph-reviewer` subagent **in parallel** — they are independent.

Pass the bundle JSON verbatim. Do not summarise it, do not strip `facts`, and do
not add repository context of your own: the whole design rests on the agent
receiving resolved facts rather than hunting for them.

| `route` | Model | Effort | Also |
|---|---|---|---|
| `human+top` | top tier | `max` | **flag for required human review** |
| `full` | top tier | `high` | |
| `light` | cheap tier | `low` | |

Bundles with `in_graph: false` still get reviewed — absence from the graph is not
evidence of safety.

## 4. Collect and report

Each agent returns a JSON array of findings. Concatenate, keep bundle order, and
report:

- every `blocker` and `should_fix`, grouped by file
- the tier-0 summary from step 2 (what was resolved for free)
- tier-1 token spend from `stats.system1_usage`
- **an explicit human-review flag** if any bundle was `sensitive`

## Headless alternative

Steps 3–4 can run outside an interactive session — same agent, same prompt:

```bash
python "${CLAUDE_PLUGIN_ROOT}/agent.py" --bundles /tmp/bundles.json --repo .
echo $?     # 1 = blockers or sensitive bundles present
```

## Rules

- **Never skip a bundle to save tokens.** The saving already happened at tier 0;
  skipping here loses coverage silently.
- **Do not review the dropped hunks.** They were resolved deterministically.
- `risk_hint` orders the work. It is not a verdict and must not be reported as one.
- A tier-1 outage escalates, never downgrades — if `system1_ok` is short, say so.
