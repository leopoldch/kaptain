# Experiment runner

Code that replays a submission plan, collects the scheduler and kaptain metrics, and produces
the per-run outputs the protocols expect. **Not implemented yet.**

The protocols themselves are documents, not code: see
[`../docs/experiments/`](../docs/experiments/). The first one to implement is the
integration-cost pilot, which needs an arrival generator, a metrics collector for both
endpoints, and the paired analysis over `summary.csv`.
