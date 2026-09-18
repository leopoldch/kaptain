# Decision records

One file per decision, numbered in the order taken. Format: context, decision, consequences,
and what would make us revisit it. A superseded record keeps its text and gains a
`Superseded by` line.

| # | Decision | Status |
|---|---|---|
| [0001](0001-two-integration-paths.md) | Keep both an HTTP extender and an in-scheduler plugin | Accepted |
| [0002](0002-score-not-filter.md) | The plugin expresses its decision as a score, never as a constraint | Accepted |
| [0003](0003-shared-snapshot-contract.md) | One snapshot contract, shared by both paths and pinned by fixtures | Accepted |
| [0004](0004-explicit-fallback.md) | Every failure produces an explicit, logged, counted fallback | Accepted |
| [0005](0005-stable-task-identity.md) | Seeded decisions key on a stable task identity, not the pod UID | Accepted |
| [0006](0006-extender-scale-quantisation.md) | Both paths quantise raw scores onto the extender's 0..10 scale | Accepted |
| [0007](0007-no-solver-direction.md) | Drop the constraint-solver direction (Christensen) | Accepted |
| [0008](0008-first-experiment-is-integration-cost.md) | The first experiment measures integration cost at equal policy | Accepted |
| [0009](0009-pluggable-telemetry-collector.md) | The telemetry collector is pluggable and names itself | Accepted |
