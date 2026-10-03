# Changelog

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
