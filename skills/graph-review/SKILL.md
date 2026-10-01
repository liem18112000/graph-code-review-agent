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

## 3. Dispatch

```bash
python "${CLAUDE_PLUGIN_ROOT}/agent.py" --bundles /tmp/bundles.json --repo .
echo $?     # 1 = blockers introduced, or sensitive bundles present
```

That is the whole step. `agent.py` reads the file, runs one `graph-reviewer` per
bundle in parallel on the subscription, picks the model from each bundle's
`route`, and prints findings on stdout with accounting on stderr.

**Never hand-copy bundle JSON into a prompt** — copying it is how you corrupt
it. If you dispatch the subagent yourself for live progress, pass it the
**path and the bundle id** and let it `Read`. Reading a prepared fact file is
not exploration; the no-search rule holds because the agent has no search tool.

| `route` | Model | Also |
|---|---|---|
| `human+top` | top tier | **flag for required human review** |
| `full` | top tier | |
| `light` | `--cheap-model` | off unless the account can reach a cheaper model |

`agent.py` warns on stderr when bundles route `light` but no cheap model is
configured — that means the tiering lever did not fire and they ran at full cost.

Bundles with `in_graph: false` still get reviewed — absence from the graph is not
evidence of safety.

## 4. Report

Findings arrive sorted: introduced before pre-existing, then by severity. Report:

- every **introduced** `blocker` and `should_fix`, grouped by file
- **pre-existing** findings in a separate, marked section — real defects this
  diff did not cause. Mixed in, they read as a merge-blocking wall. Never drop.
- the tier-0 summary from step 2 (what was resolved for free)
- tier-1 token spend from `stats.system1_usage`
- **an explicit human-review flag** if any bundle was `sensitive`

Any token figure you quote for the *current* session is a lower bound — the turn
that counts the tokens has not been billed yet when it counts them.

## Rules

- **Never skip a bundle to save tokens.** The saving already happened at tier 0;
  skipping here loses coverage silently.
- **Do not review the dropped hunks.** They were resolved deterministically.
- `risk_hint` orders the work. It is not a verdict and must not be reported as one.
- A tier-1 outage escalates, never downgrades — if `system1_ok` is short, say so.
