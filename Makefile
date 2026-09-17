CLUSTER      := kaptain
IMAGE        := kaptain-extender:dev
PLUGIN_IMAGE := kaptain-scheduler:dev
K8S_VERSION  := v1.31.0

.PHONY: up build load deploy demo logs verify down all \
        build-plugin load-plugin deploy-plugin demo-plugin logs-plugin verify-plugin \
        metrics-plugin metrics-extender demo-default test test-extender test-plugin \
        fmt-plugin lint-manifests quantities \
        replay-plugin replay-extender smoke-extender smoke-plugin pilot analyse \
        test-experiments all-plugin

# --- Cluster -----------------------------------------------------------------

# 1. Create the local multi-node cluster
up:
	kind create cluster --name $(CLUSTER) --config kind-cluster.yaml

down:
	kind delete cluster --name $(CLUSTER)

# --- Path A: HTTP extender ---------------------------------------------------

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

# --- Path B: in-scheduler Go plugin ------------------------------------------
# Compiled against Kubernetes $(K8S_VERSION), which must match kind-cluster.yaml.

build-plugin:
	docker build -t $(PLUGIN_IMAGE) plugin/

load-plugin:
	kind load docker-image $(PLUGIN_IMAGE) --name $(CLUSTER)

deploy-plugin:
	kubectl apply -f k8s/scheduler-plugin-config.yaml
	kubectl apply -f k8s/scheduler-plugin.yaml

demo-plugin:
	kubectl apply -f k8s/demo-workload-plugin.yaml

logs-plugin:
	kubectl -n kube-system logs -l app=kaptain-scheduler -f

verify-plugin:
	@echo "== Demo pods and their node =="
	kubectl get pods -l integration=plugin -o wide
	@echo "\n== Who scheduled demo-plugin? (must be kaptain-scheduler) =="
	kubectl get event --field-selector involvedObject.name=demo-plugin | grep -i scheduled || true

# The image is distroless: no shell and no wget inside. Scrape from the host instead.
metrics-extender:
	@kubectl -n kube-system port-forward deploy/scheduler-extender 8888:8888 >/dev/null 2>&1 & \
	forward=$$!; \
	trap "kill $$forward 2>/dev/null" EXIT; \
	until curl -s http://127.0.0.1:8888/healthz >/dev/null 2>&1; do sleep 0.2; done; \
	curl -s http://127.0.0.1:8888/metrics | grep kaptain_extender

metrics-plugin:
	@kubectl -n kube-system port-forward deploy/kaptain-scheduler 10259:10259 >/dev/null 2>&1 & \
	forward=$$!; \
	trap "kill $$forward 2>/dev/null" EXIT; \
	until curl -sk https://127.0.0.1:10259/healthz >/dev/null 2>&1; do sleep 0.2; done; \
	curl -sk https://127.0.0.1:10259/metrics | grep kaptain_plugin

all-plugin: up build-plugin load-plugin deploy-plugin demo-plugin

# --- Replay: identical snapshots through both paths ---------------------------
# Comparing the paths live also compares two views of the cluster. Replay removes that
# and leaves the decision cost. SNAPSHOT defaults to a shared fixture.

SNAPSHOT ?= testdata/snapshots/heterogeneous.json
STRATEGY ?= largest-cpu-capacity

replay-plugin:
	cd plugin && go run ./cmd/kaptain-replay -strategy $(STRATEGY) -snapshot ../$(SNAPSHOT) -repeat $(or $(REPEAT),1)

replay-extender:
	@python3 -c "import json,sys; print(json.dumps(json.load(open('$(SNAPSHOT)')).get('snapshot') or json.load(open('$(SNAPSHOT)'))))" \
		| curl -sS -X POST -H 'Content-Type: application/json' --data-binary @- \
		  http://127.0.0.1:8888/replay

# --- Pilot: integration cost at equal policy ------------------------------------
# Protocol: docs/experiments/pilot-integration-cost.md. One arm at a time, always.

PLAN     ?= experiments/plan.json
RUNS     ?= experiments/runs
SCENARIO ?= A-light

$(PLAN):
	cd experiments && python3 plan.py plan.json

# A handful of pods on one arm, to check the path actually places before measuring anything.
smoke-extender: $(PLAN)
	cd experiments && python3 -c "from plan import steady_then_bursts; \
	  steady_then_bursts(name='smoke', steady_count=6, steady_interval_s=0.5, bursts=1, burst_size=4).write(__import__('pathlib').Path('smoke.json'))"
	cd experiments && python3 run.py --arm extender --plan smoke.json --out runs/smoke-extender --scenario smoke

smoke-plugin: $(PLAN)
	cd experiments && python3 -c "from plan import steady_then_bursts; \
	  steady_then_bursts(name='smoke', steady_count=6, steady_interval_s=0.5, bursts=1, burst_size=4).write(__import__('pathlib').Path('smoke.json'))"
	cd experiments && python3 run.py --arm plugin --plan smoke.json --out runs/smoke-plugin --scenario smoke

# One pair. PAIRS=10 for the real thing; arms alternate so machine drift affects both.
PAIRS ?= 1
pilot: $(PLAN)
	@cd experiments && for pair in $$(seq 1 $(PAIRS)); do \
	  printf '\n== pair %s/%s ==\n' "$$pair" "$(PAIRS)"; \
	  python3 run.py --arm extender --plan plan.json --scenario $(SCENARIO) \
	    --out runs/$(SCENARIO)-$$pair-extender || exit 1; \
	  python3 run.py --arm plugin   --plan plan.json --scenario $(SCENARIO) \
	    --out runs/$(SCENARIO)-$$pair-plugin   || exit 1; \
	done

analyse:
	cd experiments && python3 analyse.py runs --out summary.csv

# The runner itself uses only the standard library; pytest comes from uv for the tests.
test-experiments:
	cd experiments && uv run --with pytest python -m pytest test_experiments.py -q

# --- Tests -------------------------------------------------------------------

test: test-extender test-plugin test-experiments

test-extender:
	cd bridge && uv run --group dev pytest -q

test-plugin:
	cd plugin && gofmt -l . && go vet ./... && go test ./...

fmt-plugin:
	cd plugin && gofmt -w .

# Regenerate the reference quantity conversions from apimachinery.
quantities:
	cd plugin && go run ./hack/qcheck

# Parse every manifest with the Kubernetes YAML decoder (no cluster needed).
lint-manifests:
	cd plugin && go build -o /tmp/kaptain-parsecheck ./hack/parsecheck
	/tmp/kaptain-parsecheck
