# The out-of-process decider: a snapshot in, scores out. It does not build snapshots and
# does not apply the fallback; the plugin does both.

import json
import logging
import os
import time

from fastapi import FastAPI
from fastapi.responses import Response

import metrics as decider_metrics
from helpers import INTEGRATION
from strategies import get_strategy

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("kaptain.decider")

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
    # `strategy` names what actually scored, not what the caller believes is deployed here:
    # the plugin records it, so a run cannot report a policy that never ran.
    # The line has to survive the failure it exists to explain: a policy that raises is
    # exactly the decision someone will come back to, and the plugin only records that the
    # call failed. Timed and logged on both paths, with the status on the histogram.
    started = time.perf_counter()
    try:
        scores = strategy.scores(snapshot)
    except Exception:
        elapsed = time.perf_counter() - started
        decider_metrics.handler_duration.labels("decide", "error").observe(elapsed)
        log.exception(json.dumps({
            "decision_id": snapshot.get("decision_id", ""),
            "run_id": snapshot.get("run_id", "unset"),
            "strategy": strategy.name,
            "duration_ms": round(elapsed * 1000, 3),
            "status": "error",
            "event": "decide",
        }))
        raise

    elapsed = time.perf_counter() - started
    decider_metrics.handler_duration.labels("decide", "ok").observe(elapsed)
    log.info(json.dumps({
        "decision_id": snapshot.get("decision_id", ""),
        "run_id": snapshot.get("run_id", "unset"),
        "strategy": strategy.name,
        "scores": scores,
        "duration_ms": round(elapsed * 1000, 3),
        "status": "ok",
        "event": "decide",
    }))
    return {"scores": scores, "strategy": strategy.name}
