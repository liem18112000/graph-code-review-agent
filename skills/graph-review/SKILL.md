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

This runs tier 0 (free path globs + whitespace detection) **and** tier 1 triage.
Just run it — do not check for `TYPESAFE_API_KEY` first and do not pre-emptively
add `--no-system1`. Tier 1 defaults to a self-hosted Laya classifier
(`laya-serve` at `http://localhost:8000/v1/systemone`), which needs **no key at
all**, so the common case needs nothing from you.

**Never add `--no-system1` just because a key looks absent.** That flag disables
tier 1 *entirely* — every bundle then goes to the top model at full cost, which
silently throws away the whole cost thesis of this plugin. A missing key is only
ever a real problem in one specific case, and the command's own exit code tells
you so:

- **Exit 0** — tier 1 ran (against Laya, or against Jev if configured with a
  key). Proceed to step 3 normally.
- **Exit 2** — this repo's `SYSTEM1_URL` is explicitly set to the *hosted* Jev
  vendor, and `TYPESAFE_API_KEY` is not set. This is the one real
  misconfiguration. Ask the user, with `AskUserQuestion`, rather than guessing:

  > No TypeSafe API key found, and this repo is configured to use the hosted
  > Jev vendor. What would you like to do?
  > 1. Provide a key now — save it to the environment, then retry with Jev.
  > 2. Use the self-hosted Laya default instead — unset `SYSTEM1_URL`, then retry.

  On (1), save the key the way the platform expects (`setx TYPESAFE_API_KEY
  "<key>"` on Windows — tell the user to reopen their shell afterward;
  `export TYPESAFE_API_KEY=<key>` added to the shell profile elsewhere) and
  re-run step 2 unchanged. On (2), unset `SYSTEM1_URL` for this session and
  re-run — tier 1 then runs against Laya, no key needed. Either way, re-run
  the plain command again; do not fall back to `--no-system1`.

Report `stats`: hunks dropped free, bundles produced, `system1_ok`,
`graph_freshness`, and `system1_usage` (tier-1 token spend).

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

## 4. Report what tier 1 decided

Each finding carries a `verdict` — `block`, `fix` or `note` — chosen by tier 1,
not by the reviewer and not by you. **Report that decision; do not re-litigate
it.** You supplied the reasoning, system 1 made the call, and `verdict_by` says
which: `system1`, or `fallback:…` when tier 1 was down or under-confident and
the reviewer's own severity stood in.

This is deliberate. A merge gate driven by whatever adjectives a reasoning model
reached for drifts run to run; a cheap classifier over a fixed record does not.
If you disagree with a verdict, say so in one line beside it and leave it alone.

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
