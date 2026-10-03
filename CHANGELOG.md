# Changelog

## 0.4.1

### Changed — self-hosted Laya is now the default, not the opt-in

`system1.DEFAULT_URL` was the hosted Jev vendor; it is now
`http://localhost:8000/v1/systemone` (a local `laya-serve`), with
`DEFAULT_MODEL = "laya"`. `TYPESAFE_API_KEY` is only required when
`SYSTEM1_URL` explicitly opts into the hosted Jev vendor
(`system1.requires_key()` centralizes that check; `review.py` and
`ensure.py` both use it).

Prompted by a real review run where tier 1 was silently skipped entirely
(`--no-system1`, every bundle on the top model) because nothing in that
environment had `SYSTEM1_URL` set — the previous default assumed a hosted
vendor and a key, so no configuration meant a hard `exit 2` unless bypassed.
Now no configuration means tier 1 just works, against the self-hosted
default.

**The honest cost of this default, measured, not assumed:** Laya's
un-tuned confidence (0.03–0.07 against a real bundle) sits far under the
0.70 routing floor, so **every bundle now escalates to `full`/`human+top`
by default** until Laya is fine-tuned against real history or `SYSTEM1_URL`
opts into Jev. Quality is unaffected — system 2 still reviews everything,
same as any tier-1 outage (§8 degrade-never-downgrade) — but the cost lever
tier 1 exists for doesn't pay off out of the box. `docs/architecture.md`
§4.1 lays out the full trade.

## 0.4.0

### Added — every "Not built" row in the design doc that was safe to build

Eight items, each verified individually and through the real pipeline (real
`graphify` graph, real local Laya, real `claude -p`):

- **Graph freshness, enforced.** `graph.json` already carried a
  `built_at_commit` stamp nothing read; `review.py` now compares it to the
  repo's actual HEAD and warns loudly on stderr instead of silently degrading
  match precision.
- **`path_to_sensitive`, fixed (§9.3).** The original `graphify path` walked
  all relation types, undirected, through test files — noise. Replaced with
  `Graph.hops_to_sensitive()`: directed `calls` edges only, test files
  excluded, capped at 4 hops. A bundle one call away from a sensitive path now
  gets a `rank()` bonus instead of nothing.
- **System-1 hints to the reviewer.** `agent.py hints_for()` crosses over
  `risk` and `security_concern` only — never `route`, never the confidence
  value — each phrased as a claim the agent prompt must confirm or refute,
  never a conclusion it inherits.
- **Opt-in linter/SAST wiring.** `review.py --run-linters` shells out to
  `[[linters]]` configured in `overrides.toml`, once per matching file among
  the surviving hunks. Results carry a verdict already and skip both models
  entirely (`agent.py lint_findings_from()`).
- **Feedback loop.** `feedback.py log`/`stats`, append-only JSONL, no server —
  logs human accept/dismiss per finding and reports accept rate by category,
  severity, and whether tier 1's own verdict or the fallback mapping was used.
- **Queue-level PR ranking**, since `graphify prs --triage` does not exist in
  the installed graphify. `rank_queue.py` reuses `review.py`'s own `build()`
  per ref against a common base — no new ranking logic.
- **An MCP server**, since graphify's own MCP mode does not exist either.
  `mcp_server.py` is a hand-rolled stdio JSON-RPC server, stdlib only,
  exposing `review_diff` and `rank_queue` as tools.
- **`bench.py selftest`**, synthetic, no `defects4j` or network needed.

**Deliberately left not-built:** tier-1 skip-on-low-risk, a separate verifier
stage, and a second reviewer on high-risk bundles. All three are gated on
benchmark evidence §7/§10/§12 say doesn't exist yet — building them now would
be resolving an open question by fiat instead of by measuring.

### Removed — the Defects4J benchmark path

`bench.py prepare` / `prepare_bug()` and `setup-defects4j.sh` are gone.
`prepare-git` was already the primary, decisive arm (§10.1: own history, no
multi-GB download) and nothing else in this repo depended on the Defects4J
path. The design discussion of why Defects4J would still be useful as a
contamination-control arm stays in `docs/architecture.md` — running it now
just means standing it up by hand, per its own docs, instead of through this
repo.

## 0.3.0

### Added — tier-1 can be self-hosted (Laya) with no API key

`SYSTEM1_URL` already pointed anywhere per the §4.1 swap seam, but
`review.py` and `ensure.py` both still hard-required `TYPESAFE_API_KEY`
regardless of the URL — a self-hosted endpoint (e.g. `laya-serve`, the
Apache-2.0 [`convaiinnovations/laya`](https://huggingface.co/convaiinnovations/laya)
model) was reachable but unusable without a Jev key it doesn't need. Both
scripts now gate the key only against the hosted default; a custom
`SYSTEM1_URL` needs nothing else. `ensure.py --deep` verified against a real
local `laya-serve`, which also surfaced the honest limitation already
documented in §4.1: Laya's un-tuned confidence (0.03–0.07, measured) sits
well under the 0.70 routing floor, so every bundle degrades to `human+top` —
correct behaviour, not a bug.

### Added — the reviewer also flags unneeded complexity

`graph-reviewer` gained a seventh review dimension: new interfaces,
wrappers, config knobs or dependencies the diff didn't need, grounded the
same way as every other finding — a `callers: 1` fact is now also read as
"this abstraction has exactly one caller." Findings carry a new `category`
field (`correctness | security | concurrency | data-access |
breaking-change | test-coverage | complexity`); nothing downstream that
parses findings needed to change. Implemented entirely in the agent's own
prompt, independent of any other over-engineering reviewer that might also
run in the environment.

### Fixed — `--no-verdict` silently defeated the merge gate

With `--no-verdict`, no finding ever got a `verdict` key, so
`blockers = sum(f.get("verdict") == "block" ...)` was always `0` — an
introduced blocker would exit 0 and merge. `agent.py` now applies the same
`FALLBACK` severity mapping and `pre_existing` clamp that `adjudicate()`
already uses when tier 1 is down, instead of skipping verdict assignment
entirely. Pinned by `test_agent.py`, a new assert-based self-check for this
gate path.

### Fixed — an unvalidated tier-1 choice could reach the gate unchecked

`adjudicate()` accepted whatever string tier 1 returned as `verdict`
without checking it was one of `block|fix|note`. An unexpected choice now
falls back to the severity mapping instead of passing through.

## 0.2.0

### Fixed — the escalation ladder never actually fired

Tier 1 returned `risk=low` with **confidence 0.33** on exactly the bundles that
should route `light`, so every one escalated and the cheap tier was never used —
in the benchmark, in a synthetic probe, and in production.

Cause: the instructions said to read unknown callers as a sign the runtime
invokes the code directly, while the `low` criterion mentioned only *known*
callers. The conflict bit only when everything else pointed to low, so it was
invisible in the other verdicts. Criteria now state what unmeasured callers mean
for each level. Measured on the same commit: **0.33 → 0.99**, `human+top` →
`light`.

### Added — `scope` on every finding

Findings carry `scope: introduced | pre_existing`. Serving the graph
neighbourhood makes the reviewer review the neighbourhood: on a real bundle
**5 of 7 findings were pre-existing defects this diff did not cause**.

**Behaviour change:** pre-existing findings sink in the sort and no longer fail
CI. A blocker in code your diff did not touch will not break your build.

### Added — tier-1 verdict on each finding

Each finding goes back to tier 1 and comes back `block | fix | note`;
`agent.py` applies that choice and the exit code follows it.

**Measured honestly, and it has not yet earned its place.** Over 12 findings the
merge gate was *identical* with and without it — 0 blocks either way — and every
difference was a `pre_existing` demotion derivable from a field already in the
record. Tier 1 is confident (0.82–1.00) on `note`, which a deterministic rule
already handles, and unconfident (0.27–0.72) on `block` vs `fix`, which needs
production impact it cannot see.

Pass `--no-verdict` to skip it and keep the reviewer's own severity. Doing so
costs you nothing measurable and saves ~660 tokens per finding.

### Changed

- `--max-bundle` **6 → 12**. Each bundle is one agent paying a cold prompt-cache
  write regardless of size (24% of subagent tokens, measured); at 6 an 8-hunk
  package was split into two agents for nothing.
- `TYPESAFE_API_KEY` now also resolves from the Windows registry. `setx` writes
  there and a process started earlier never inherits it, so the key could be set
  and invisible at the same time.
- `agent.py` warns when bundles route `light` but no cheap model is configured —
  the tiering lever used to fail silently and run them at full cost.
- The skill dispatches via `agent.py` instead of asking the model to retype
  bundle JSON into subagent prompts. A transcription slip in the old path cost a
  rerun.

## 0.1.0

Initial release.
