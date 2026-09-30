# graph-code-review-agent

A Claude Code plugin that reviews diffs **from a code graph instead of from grep**.

Most high-effort review setups fan out N reviewers over the same diff, and each one
independently greps the repo to discover callers, callees and tests. Those facts get
rediscovered N times, by an LLM, at full token price — and every grep is a serial
round-trip, so it costs wall-clock too.

This plugin computes those facts once, deterministically, for zero tokens.

```
tier 0   path globs, whitespace, lockfiles      free, no model
tier 1   fast classifier scores each bundle     ~$0.0000  per bundle
tier 2   reasoning model, only where earned     the expensive part
```

On a real 26-hunk diff: **13 hunks resolved free**, tier 1 cost **$0.00014** for all
four bundles, and only what survived reached the reasoning model.

---

## Requirements

| | Why |
|---|---|
| **Claude Code** | Runs the reviewer on your **subscription** — no `ANTHROPIC_API_KEY` |
| **Python 3.11+** | `tomllib` is stdlib from 3.11 |
| **`graphify`** on `PATH` | Builds the AST graph (local parse, no tokens) |
| **`TYPESAFE_API_KEY`** in the environment | Tier-1 triage. The only API key this system uses. |

Nothing else — no pip install, no virtualenv. The scripts are stdlib only.

---

## Install

```bash
claude plugin marketplace add liem18112000/graph-code-review-agent
claude plugin install graph-code-review-agent
```

Then in the repo you want to review, once:

```bash
graphify update .          # build the graph
graphify hook install      # keep it current on every commit
```

Verify the key is visible to Claude Code:

```bash
echo $TYPESAFE_API_KEY     # must be set; see "Key resolution" below
```

---

## Use

**Interactive** — in any repo with a graph:

```
/graph-review
```

**Headless** — CI, a git hook, or a shell:

```bash
git diff origin/main...HEAD > pr.diff

python review.py --diff pr.diff --graph graphify-out/graph.json --repo . > bundles.json
python agent.py  --bundles bundles.json --repo . > findings.json

echo $?   # 1 = blockers found, or a sensitive path needs a human
```

`agent.py` exits non-zero on a blocker **or** an untriaged sensitive path, so it
gates a pipeline as-is.

---

## What it gives the reviewer

Each bundle of related hunks arrives with its graph neighbourhood already resolved:

```json
{
  "id": "b001",
  "package": "src/main/java/com/acme/orders/import",
  "sensitive": true,
  "facts": {
    "entry_point_annotations": ["@ObservesAsync"],
    "symbols": [{"symbol": ".receiveMessage()", "callers": "unknown",
                 "match_precision": "file", "callees": [".handleMessage()"]}],
    "covering_tests": [],
    "in_graph": true
  },
  "hunks": [...]
}
```

The agent is granted `Read` and **no search tool**, deliberately. It is told the
facts are resolved and not to go looking — which is safe only because they really
are resolved.

### `callers: "unknown"` is not zero

On a framework-wired codebase (CDI, Spring, JAX-RS) most methods have **no callers
in a static graph** — measured at 66% on a real Quarkus service. Zero callers there
means *the runtime invokes it*, which is the high-risk case, not the safe one.

So fan-in of zero is reported as `unknown`, never `0`, and entry-point status is
scanned from **source annotations** rather than inferred from graph topology.

---

## Key resolution

`TYPESAFE_API_KEY` is read from the environment first, then from a `.env` beside
the scripts. A shell `export` does not survive into CI, a git hook, or another
tool's subprocess — the `.env` fallback exists for exactly those cases.

```bash
# .env  (gitignored — never commit this)
TYPESAFE_API_KEY=...
```

Two failure modes, handled differently on purpose:

| Situation | Behaviour |
|---|---|
| Key absent — **misconfiguration** | Exit 2 before any work, with the fix in the message |
| Key present, endpoint down — **outage** | Degrade and **escalate**; never downgrades a bundle to the cheap tier |

---

## Swapping the classifier

Tier 1 is defined by an HTTP contract, not a vendor. Point it elsewhere with one
variable:

```bash
SYSTEM1_URL=https://api.typesafe.ai/v1/systemone   # default
SYSTEM1_URL=http://localhost:8000/v1/systemone     # a self-hosted alternative
```

The payload is a **compact structural record** — file and symbol names, caller
counts, test counts. **No diff, no source.** A runtime guard in `system1.py` raises
if diff text ever reaches it, which is what makes an external classifier acceptable
against a proprietary codebase.

---

## Layout

```
.claude-plugin/     plugin + marketplace manifests
agents/             graph-reviewer.md — the reviewer prompt and tool grant
skills/             graph-review — the interactive orchestrator
review.py           diff -> hunks -> graph facts -> ranked bundles
system1.py          tier-1 client (the swappable slot)
agent.py            headless driver: one `claude -p` per bundle
diffparse.py        unified-diff parsing
bench.py            A/B replay harness for measuring against a baseline
overrides.toml      tier-0 globs: sensitive / skip / tests
docs/               architecture.md (why) and call-flow.md (what calls what)
```

---

## Measuring it

`bench.py` prepares tasks and scores answers but never invokes a reviewer, so it
stays neutral between arms:

```bash
bash setup-defects4j.sh                      # one-time, multi-GB
python bench.py prepare --bugs Lang:1,Math:5 --out tasks/
python bench.py score --tasks-dir tasks/ --findings-dir out/graph/
```

It reports `localization_rate`, `mean_findings_per_bug`, and
`mean_rank_of_first_hit` — rank matters because reviews are read top-down. A
missing findings file counts as a **miss**, never a skip, so an arm that crashes
cannot be flattered by its own failure.

---

## Status

Working and measured end to end. Known gaps, stated plainly:

- **The cheap-model tier is built but unexercised.** No bundle in testing has yet
  cleared the confidence floor for the `light` route.
- **Benchmark numbers are not in yet.** The harness exists; the A/B against a
  baseline reviewer has not been run.
- Subscription auth does not travel to hosted CI runners. Works on a dev machine
  or a self-hosted runner where you are logged in.

MIT.
