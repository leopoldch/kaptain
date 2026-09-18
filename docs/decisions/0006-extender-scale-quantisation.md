# 0006 — Both paths quantise raw scores onto the extender's 0..10 scale

**Status:** accepted, 2026-09-17.

## Context

The two paths had different score resolutions. The extender returns integers in
`[0, MaxExtenderPriority]`, which Kubernetes multiplies by `MaxNodeScore/MaxExtenderPriority`
(so 0..10, then ×10). The plugin normalised straight into 0..100.

With raw scores 1000, 1960 and 2000, the extender produced 0, 10, 10 — a tie between the last
two, broken at random — while the plugin produced 0, 96, 100 and picked the third node
cleanly. Same policy, same snapshot, different placement. The comparison would have measured
that, not the integration.

Quantising the already-scaled int64 scores had a second flaw: any raw score below
1/`scorePrecision` collapsed to zero, so 0.0001 and 0.0002 became a tie. The shared table
caught it.

## Decision

Both paths quantise the **raw** scores onto `[0, MaxExtenderPriority]` with identical integer
truncation, and the plugin then applies the ×10 itself. `testdata/quantisation.json` pins the
rule case by case, including the near-tie above, and both suites walk it.

## Consequences

- Identical scores, identical winner, identical ties on both paths.
- The plugin deliberately gives up resolution it could have. A finer scale would let it break
  ties the extender cannot even express, which is a difference in policy expressiveness
  masquerading as a difference in integration.
- Ties are then a shared property, and are reported: `tie_count` per decision, and the
  `PostBind` line for what the scheduler actually did with them.

## Revisit if

The extender path is abandoned, or an experiment needs fine-grained score comparison (ranking
quality rather than placement). The plugin could then normalise into the full 0..100, and any
result mixing the two scales would have to say so.
