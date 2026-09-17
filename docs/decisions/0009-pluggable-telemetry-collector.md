# 0009 — The telemetry collector is pluggable and names itself

**Status:** accepted, 2026-09-17. Raised in review of PR #6.

## Context

Measured node usage currently travels a long path:

```text
kubelet → metrics-server → metrics.k8s.io → kaptain cache → scheduler
```

metrics-server is a sensible first source: it is the standard API, it is already deployed in
most clusters, and it keeps the collection code small. But for a plugin running *inside* the
scheduler, it is also an intermediate dependency that adds staleness and narrows what we can
see to CPU and memory. The kubelet exposes richer per-node, per-pod and per-container data
through its summary and resource-metrics endpoints, which a collector could read directly:

```text
kubelet → kaptain cache → scheduler
```

Whether that is worth it is an empirical question — how much staleness does metrics-server
actually add, and does the extra data change any decision? — and it is one we cannot answer
by arguing. It needs both collectors to exist side by side.

## Decision

Keep `metrics.k8s.io` as the only implemented collector, and make the collector a
configuration choice behind a stable interface:

- `telemetry.Source` in Go, the equivalent module boundary in Python. It exposes `Get(node)`
  and `Name()`.
- `telemetry.Open(ctx, collector, opts)` / `telemetry.open_source(collector, …)` select it
  from `KAPTAIN_TELEMETRY`: `off`, `metrics-api`, and `kubelet` **reserved**.
- An unimplemented or unknown collector is refused with an explicit error. It never falls
  back silently to another source, because a run would then record the wrong provenance.
- The collector names itself. The name goes into the startup line, into `/healthz` for the
  extender, and into the `source` label of `cache_age_seconds`, so a result says which
  collector produced its numbers.

Strategies are unaffected: they read the snapshot, which carries usage and its age, and never
the collector.

## Consequences

- Adding a kubelet collector is a new file plus a case in `Open`, not a change to the
  strategies, the snapshot or the tests that pin them.
- The two collectors can then be compared as an experimental variable, on the same footing as
  the two integrations: same policy, same snapshot fields, different provenance and staleness.
- The measurement rule from [decision 0003](0003-shared-snapshot-contract.md) still holds: the
  age carried is the age of the *measurement*, from the source's own timestamp, and a sample
  without a usable timestamp is refused.
- Both integrations name the same collectors, so a run is described the same way whichever
  path it used.

## Revisit if

The kubelet collector turns out to be materially fresher or richer. `metrics-api` would then
become the compatibility option rather than the default, and the difference measured between
them belongs in the results, not in a footnote.
