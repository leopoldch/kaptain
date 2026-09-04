import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from strategies import get_strategy

strategy = get_strategy(os.getenv("STRATEGY", "dummy-random"))
app = FastAPI()


@app.get("/healthz")
def healthz():
    return {"status": "ok", "strategy": strategy.name}


@app.post("/filter")
async def filter_nodes(args: dict):
    nodes = args.get("Nodes", {}) or {}
    names = args.get("NodeNames") or [n["metadata"]["name"] for n in nodes.get("items", [])]
    return JSONResponse({"Nodes": nodes, "NodeNames": names, "FailedNodes": {}, "Error": ""})


@app.post("/prioritize")
async def prioritize(args: dict):
    pod = args.get("Pod", {})
    nodes = (args.get("Nodes") or {}).get("items", [])
    if not nodes:
        return JSONResponse([])
    try:
        chosen = strategy.select(pod, nodes)
    except Exception:
        chosen = None
    return JSONResponse(
        [{"Host": n["metadata"]["name"], "Score": 10 if n["metadata"]["name"] == chosen else 0}
         for n in nodes]
    )
