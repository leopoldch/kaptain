# 0002 — The plugin expresses its decision as a score, never as a constraint

**Status:** accepted, 2026-09-17.

## Context

A Scheduling Framework plugin can impose a placement in two ways. It can score candidates and
let the scheduler rank them, or it can filter: mark every node but the chosen one
unschedulable. DRS (Jian et al. 2024) does the second — `dqn-plugin` is a `Filter` — which we
read in its source before deciding.

Filtering is tempting because it guarantees the decision is applied. But it changes the
problem: admissibility stops meaning "this pod can run here" and starts meaning "our policy
preferred elsewhere". The scoring phase becomes inert, so the scheduler's own tie-breaking,
preemption and fallback behaviour no longer apply. In DRS this has a visible consequence:
when the decider is unreachable, every node passes the filter and the default scoring decides
silently.

## Decision

The plugin implements `PreScore`, `Score` and `NormalizeScore`, plus `Reserve`/`Unreserve`
and `PostBind`. It does not implement `Filter`. Admissibility stays Kubernetes' job.

## Consequences

- The policy is comparable with the extender, which can only score anyway.
- The decision is a preference, not a guarantee: on a tie, kube-scheduler picks at random
  among the top nodes. This is why the log distinguishes `intended_node` from the `PostBind`
  binding line, and why `kaptain_plugin_bound_mismatch_total` exists.
- A profile test asserts the plugin is enabled at `preScore`, `score`, `reserve` and
  `postBind`, and **not** at `filter`, so a future edit cannot quietly turn the decision into
  a constraint.

## Revisit if

An experiment genuinely needs a forced placement — an ablation where the policy must be
applied exactly. Even then, a separate profile is preferable to changing this one, and the
document describing it must say that the decision has become a constraint.
