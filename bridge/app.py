# The out-of-process decider: a snapshot in, scores out. It does not build snapshots and
# does not apply the fallback; the plugin does both.

import logging
import os
import time

from fastapi import FastAPI
from fastapi.responses import Response

import metrics as decider_metrics
from helpers import INTEGRATION
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
    # `strategy` names what actually scored, not what the caller believes is deployed here:
    # the plugin records it, so a run cannot report a policy that never ran.
    started = time.perf_counter()
    scores = strategy.scores(snapshot)
    decider_metrics.handler_duration.labels("decide").observe(time.perf_counter() - started)
    return {"scores": scores, "strategy": strategy.name}
