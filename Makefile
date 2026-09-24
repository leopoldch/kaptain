CLUSTER      := kaptain
IMAGE        := kaptain-extender:dev
K8S_VERSION  := v1.31.0

.PHONY: up build load deploy demo logs verify down all \
        demo-default metrics-extender test test-extender replay-extender

# --- Cluster -----------------------------------------------------------------

# 1. Create the local multi-node cluster
up:
	kind create cluster --name $(CLUSTER) --config kind-cluster.yaml

down:
	kind delete cluster --name $(CLUSTER)

# --- HTTP extender ------------------------------------------------------------

# 2. Build the Python service image (adapter + strategies)
build:
	docker build -t $(IMAGE) bridge/

# 3. Load the image into kind (no external registry needed)
load:
	kind load docker-image $(IMAGE) --name $(CLUSTER)

# 4. Deploy extender + second scheduler
deploy:
	kubectl apply -f k8s/scheduler-config.yaml
	kubectl apply -f k8s/extender.yaml
	kubectl apply -f k8s/scheduler.yaml

# 5. Run the demo pod for this arm. One arm at a time: see the manifest comments.
demo:
	kubectl apply -f k8s/demo-workload.yaml

demo-default:
	kubectl apply -f k8s/demo-workload-default.yaml

# Watch the extender being called (scores flow through here)
logs:
	kubectl -n kube-system logs -l app=scheduler-extender -f

# Check that our scheduler placed the pod
verify:
	@echo "== Demo pods and their node =="
	kubectl get pods -l app=demo -o wide
	@echo "\n== Who scheduled demo-ours? (must be ml-scheduler) =="
	kubectl get event --field-selector involvedObject.name=demo-ours | grep -i scheduled || true

all: up build load deploy demo

# The image is distroless: no shell and no wget inside. Scrape from the host instead.
metrics-extender:
	@kubectl -n kube-system port-forward deploy/scheduler-extender 8888:8888 >/dev/null 2>&1 & \
	forward=$$!; \
	trap "kill $$forward 2>/dev/null" EXIT; \
	until curl -s http://127.0.0.1:8888/healthz >/dev/null 2>&1; do sleep 0.2; done; \
	curl -s http://127.0.0.1:8888/metrics | grep kaptain_extender

# --- Replay -------------------------------------------------------------------
# Replaying a fixture removes the live cluster from the comparison and leaves the
# decision cost. SNAPSHOT defaults to a shared fixture.

SNAPSHOT ?= testdata/snapshots/heterogeneous.json
STRATEGY ?= largest-cpu-capacity

replay-extender:
	@python3 -c "import json,sys; print(json.dumps(json.load(open('$(SNAPSHOT)')).get('snapshot') or json.load(open('$(SNAPSHOT)'))))" \
		| curl -sS -X POST -H 'Content-Type: application/json' --data-binary @- \
		  http://127.0.0.1:8888/replay

# --- Tests -------------------------------------------------------------------

test: test-extender

test-extender:
	cd bridge && uv run --group dev pytest -q
