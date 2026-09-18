# 0004 — Every failure produces an explicit, logged, counted fallback

**Status:** accepted, 2026-09-17.

## Context

The extender originally answered a failed strategy with `except Exception: chosen = None`,
which scored every node 0. Since the profile disables the native scorers, all nodes were then
**perfectly tied** and kube-scheduler picked arbitrarily. A crashing strategy therefore became
a random baseline, inside the results, with nothing in the logs to say so.

The same shape exists upstream: when DRS cannot reach its decider, every node passes its
filter and the default scoring decides silently.

## Decision

Both paths validate the answer before using it, and fall back explicitly when it is unusable:

- the strategy raised, or the external decider failed, timed out or returned an unknown node;
- the score map is incomplete, or holds a value that is not finite, or is absurdly large.

The fallback itself is deterministic, needs no telemetry, and is identical on both sides:
highest free-request ratio, then most allocatable CPU, then node name.

Each fallback is logged with its reason, counted in `fallback_total{reason}`, and **kept in
the results as a decision of the method under test**.

## Consequences

- A broken policy shows up as a fallback rate, not as a mysteriously average result.
- Score validation must be strict for this to hold: an incomplete map would otherwise let a
  missing candidate be read as zero and outrank a legitimately negative score.
- The fallback is a policy too. Its rule is documented so results can account for it, and it
  degrades predictably: in the extender path per-node requests were unknown before the API
  source existed, which flattened the first criterion — hence the second one.

## Revisit if

We start measuring adaptation or robustness explicitly. A fallback that is frequent enough to
shape a result should become an arm of the comparison, not a footnote.
