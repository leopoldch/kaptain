# 0007 — Drop the constraint-solver direction

**Status:** accepted, 2026-09-17. Supersedes the anchor decision of 2026-09-08 recorded in
[related-work.md](../related-work.md).

## Context

An earlier framing made Christensen et al. 2025 the anchor: its CP-SAT solver as a teacher,
a distilled fast policy underneath, RL refinement above. The paper is also, by a distance, the
most reproducible of the corpus — code cited in the PDF, Apache-2.0, workload generator,
trace replayer and public traces included, and it runs locally on KWOK.

That reproducibility is not the same thing as fitting the study. The solver optimises
priority-aware packing with eviction and relocation across the cluster; our question is what a
learned policy is worth for a single placement, per pod, under a decision-cost budget, with
no migration and no preemption.

## Decision

Christensen is not pursued as a direction. It stays in the literature review, and remains
available as an offline packing comparator if a later experiment needs an upper bound on
packing quality — under an explicit statement that its action space is larger than ours.

## Consequences

- The solver-teacher framing is superseded. Do not propose it again without new context.
- DRS becomes the recent-literature baseline to reimplement (its formulation, not its code:
  the repository carries no licence), and Decima stays a conceptual reference, not a
  Kubernetes baseline.
- The reproducibility inventory for all eight papers, and what each one releases, is in
  [reproduction/README.md](../../../reproduction/README.md).

## Revisit if

The project needs a bound on how much placement quality is left on the table, and we accept
paying for a comparator whose actions differ from ours. The comparison would then have to be
read as "what a cluster-wide optimiser achieves with more freedom", not as our ceiling.
