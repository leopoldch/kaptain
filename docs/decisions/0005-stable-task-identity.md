# 0005 — Seeded decisions key on a stable task identity, not the pod UID

**Status:** accepted, 2026-09-17.

## Context

`dummy-random` is the control arm: a seeded draw, so that a run can be replayed exactly. It
originally keyed on the pod UID, which Kubernetes assigns at creation. Recreating the same
workload produces new UIDs, so the same seed produced different placements — the arm was
reproducible within a run and not across runs, which is the opposite of what a control needs.

## Decision

Seeded decisions key on a task identity that survives recreation, in this order: the
`kaptain.io/task-id` label, then the same annotation, then `namespace/name`, and the UID only
as a last resort.

The draw itself is FNV-1a over `seed:task-id` against the sorted candidate names, implemented
identically in Go and Python so both paths draw the same node.

## Consequences

- An experiment replays: same submission plan, same seed, same task ids, same placements on
  both integrations. This is what lets a paired comparison attribute a difference to the
  integration.
- The runner must set `kaptain.io/task-id` when pod names are generated, or rely on stable
  pod names.
- The identity is also the natural join key between the submission plan, the decision logs
  and the binding records.

## Revisit if

Two concurrent pods legitimately share a task id — a replicated workload where each replica
should draw independently. The key would then need a replica index, and the fixtures would
have to cover it.
