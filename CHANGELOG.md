# Changelog

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
