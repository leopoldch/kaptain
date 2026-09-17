# 0001 — Keep both an HTTP extender and an in-scheduler plugin

**Status:** accepted, 2026-09-17.

## Context

The project plan asks what a smarter placement policy costs to run, not only whether it
places better. An HTTP extender is the fastest way to write a policy in Python and to call a
local LLM, but every decision then crosses a process boundary and a JSON serialisation of the
node list. An in-scheduler Go plugin has neither, and is closer to how the literature
integrates its schedulers — DRS runs its DQN outside the scheduler, but its plugin lives
inside it.

Keeping only the extender would make every latency number a property of our transport.
Keeping only the plugin would make ML and LLM work awkward, since the policies are in Python.

## Decision

Build and maintain both, with the same policy running on either side.

## Consequences

- The integration becomes an experimental variable: same strategy, same snapshot, two paths,
  and the difference is the integration. Every result line and metric sample carries
  `integration=extender|plugin`.
- Anything that makes the two paths see different things is a defect, not a detail. This is
  what forced [0003](0003-shared-snapshot-contract.md) and
  [0006](0006-extender-scale-quantisation.md).
- Double the surface to maintain: two strategy implementations, two fallbacks, two metric
  sets. Shared fixtures are what keeps them honest.
- The plugin pins us to a Kubernetes version (v1.31.0): it compiles against the Scheduling
  Framework, so the kind nodes, the scheduler image and the plugin move together.

## Revisit if

The pilot in [0008](0008-first-experiment-is-integration-cost.md) shows a difference too
small to matter for our workloads. The extender would then stay the single path, and the
plugin would become a measurement artefact we keep only to justify that claim.
