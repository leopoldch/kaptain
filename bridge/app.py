# The out-of-process decider: a snapshot in, scores out. It does not build snapshots and
# does not apply the fallback; the plugin does both.

import logging
import os
import time

from fastapi import FastAPI
from fastapi.responses import Response

import metrics as decider_metrics
from snapshot import INTEGRATION, MAX_EXTENDER_PRIORITY, best, names, normalize
from strategies import get_strategy

logging.basicConfig(level=logging.INFO, format="%(message)s")

strategy = get_strategy(os.getenv("STRATEGY", "dummy-random"))

app = FastAPI()


@app.get("/healthz")
def healthz():
    return {"status": "ok", "strategy": strategy.name, "integration": INTEGRATION}


@app.get("/metrics")
def prometheus_metrics():
    return Response(decider_metrics.render(), media_type="text/plain; version=0.0.4")


@app.post("/decide")
def decide(snapshot: dict):
    # Timed here and on the caller side; the difference is the transport.
    started = time.perf_counter()
    scores = strategy.scores(snapshot)
    decider_metrics.handler_duration.labels("decide").observe(time.perf_counter() - started)
    return {"scores": scores}


@app.post("/replay")
def replay(snapshot: dict):
    # Scores a recorded snapshot without touching the cluster; kaptain-replay is the twin.
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
