# 0008 — The first experiment measures integration cost at equal policy

**Status:** accepted, 2026-09-17.

## Context

The corpus leaves four gaps, and the one we can address first is decision cost: no paper
measures it comparably, and several do not report it at all. Placement quality needs a
workload whose outcome we can attribute, models to compare and a corpus we have not chosen
yet. Decision cost needs none of that — it needs two integrations of the same policy, which
now exist and are pinned to produce identical decisions.

Running a quality experiment first would also be dishonest in a subtler way: any latency we
measured later would be entangled with whatever policy we had settled on.

## Decision

The first experiment is the pilot in
[experiments/pilot-integration-cost.md](../experiments/pilot-integration-cost.md):
`dummy-random` on both paths, a submission plan generated once and replayed, with and without
a `stress-ng` background load, ten paired runs alternating arms.

The primary metric is `scheduler_scheduling_algorithm_duration_seconds`, taken from the
scheduler itself.

## Consequences

- Two facts had to be checked in the Kubernetes source, and both shape the design. Scoring is
  skipped entirely when a single node survives filtering, so the pilot must keep at least two
  feasible nodes or it measures nothing. Extenders are called outside the framework extension
  points, so `scheduler_framework_extension_point_duration_seconds` does not contain the
  extender round trip — our earlier documentation named the wrong metric.
- Our own timers decompose a decision but cannot settle the comparison: the Python handler
  timer excludes the transport that the whole question is about.
- The conclusion will be bounded by kind: one machine, shared resources, cosmetic node
  labels. It is a real answer about this testbed, and the same protocol on separate Linux VMs
  is what turns it into a result for the paper.
- It needs the experiment runner, which does not exist. That is now the blocking item.

## Revisit if

The pilot shows the two paths within noise of each other. The question then becomes whether
the difference matters at a realistic scale, which is a bigger-cluster question, not a
kind question.
