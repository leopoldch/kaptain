import logging
import os
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from config import duration
from decision import Decider, counters, normalize
import metrics as extender_metrics
from snapshot import (FEATURE_ALLOCATABLE, FEATURE_REQUESTED, FEATURE_TELEMETRY, INTEGRATION,
                      MAX_EXTENDER_PRIORITY, best, names)
from strategies import PROTOCOL_FEATURES, get_strategy, missing_requirements

logging.basicConfig(level=logging.INFO, format="%(message)s")

strategy = get_strategy(os.getenv("STRATEGY", "dummy-random"))

# The extender protocol carries no per-node requested resources. With
# KAPTAIN_REQUESTS_SOURCE=api a background refresh reads them from the Kubernetes API, which
# is what makes a resource-aware comparison with the plugin fair.
requests_source = None
telemetry_source = None
available = PROTOCOL_FEATURES
if os.getenv("KAPTAIN_REQUESTS_SOURCE", "none") == "api":
    from cluster import ApiRequestsSource

    requests_source = ApiRequestsSource(
        refresh_seconds=duration("KAPTAIN_REQUESTS_REFRESH", "2s"),
        max_age_seconds=duration("KAPTAIN_REQUESTS_MAX_AGE", "30s"),
    )
    requests_source.start()
    available = available | {FEATURE_REQUESTED}

# Measured usage, needed by least-used (the BT arm). The collector is selectable and named,
# so a run records which one produced the numbers; see telemetry.open_source.
from telemetry import DISABLED as TELEMETRY_OFF, open_source

telemetry_source = open_source(
    os.getenv("KAPTAIN_TELEMETRY", TELEMETRY_OFF),
    refresh_seconds=duration("KAPTAIN_TELEMETRY_REFRESH", "2s"),
    max_age_seconds=duration("KAPTAIN_TELEMETRY_MAX_AGE", "30s"),
)
if telemetry_source is not None:
    available = available | {FEATURE_TELEMETRY}

# Fail fast rather than produce a run made only of fallbacks.
# KAPTAIN_ALLOW_DEGRADED=1 runs it anyway, for debugging only.
_missing = missing_requirements(strategy, available)
if _missing and os.getenv("KAPTAIN_ALLOW_DEGRADED") != "1":
    raise RuntimeError(
        f"strategy {strategy.name!r} needs {sorted(_missing)}; set "
        "KAPTAIN_REQUESTS_SOURCE=api and/or KAPTAIN_TELEMETRY=metrics-api to provide them, "
        "or KAPTAIN_ALLOW_DEGRADED=1 to force a degraded run"
    )

decider = Decider(strategy, requests=requests_source, telemetry=telemetry_source)
app = FastAPI()


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "strategy": strategy.name,
        "integration": INTEGRATION,
        "features": sorted(available),
        "telemetry_collector": getattr(telemetry_source, "name", TELEMETRY_OFF),
    }


@app.get("/stats")
def stats():
    return {
        "strategy": strategy.name,
        "integration": INTEGRATION,
        "run_id": decider.run_id,
        "policy_version": decider.policy_version,
        "counters": counters,
    }


@app.get("/metrics")
def prometheus_metrics():
    """The plugin exposes the same series on the scheduler's /metrics."""
    return Response(extender_metrics.render(), media_type="text/plain; version=0.0.4")


@app.post("/replay")
def replay(snapshot: dict):
    """Score a recorded snapshot, without touching the cluster.

    Comparing the two integrations on a live cluster also compares two views of the cluster:
    the plugin sees its own reservations in the scheduler cache, while this service refreshes
    bound pods periodically. Replaying the same snapshot through both paths removes that
    difference and leaves the decision cost. The plugin's equivalent is `kaptain-replay`.
    """
    started = time.perf_counter()
    scores = strategy.scores(snapshot)
    elapsed = (time.perf_counter() - started) * 1000
    return {
        "integration": INTEGRATION,
        "mode": "replay",
        "strategy": strategy.name,
        "run_id": snapshot.get("run_id", "unset"),
        "policy_version": snapshot.get("policy_version", "unset"),
        "pod_task_id": snapshot.get("pod", {}).get("task_id", ""),
        "intended_node": best(names(snapshot), scores),
        "scores": scores,
        "normalized": normalize(scores),
        "scale": MAX_EXTENDER_PRIORITY,
        "duration_ms": round(elapsed, 3),
    }


@app.post("/filter")
def filter_nodes(args: dict):
    """Pass-through: admissibility stays Kubernetes' job, in both integrations."""
    nodes = args.get("Nodes", {}) or {}
    names = args.get("NodeNames") or [n["metadata"]["name"] for n in nodes.get("items", [])]
    return JSONResponse({"Nodes": nodes, "NodeNames": names, "FailedNodes": {}, "Error": ""})


@app.post("/prioritize")
def prioritize(args: dict):
    pod = args.get("Pod", {})
    nodes = (args.get("Nodes") or {}).get("items", [])
    if not nodes:
        return JSONResponse([])
    decision = decider.decide(pod, nodes)
    return JSONResponse([
        {"Host": name, "Score": score} for name, score in decision.normalized.items()
    ])
