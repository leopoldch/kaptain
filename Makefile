CLUSTER      := kaptain
IMAGE        := kaptain-extender:dev
PLUGIN_IMAGE := kaptain-scheduler:dev
K8S_VERSION  := v1.31.0

.PHONY: up build load deploy demo logs verify down all \
        build-plugin load-plugin deploy-plugin demo-plugin logs-plugin verify-plugin \
        metrics-plugin metrics-extender demo-default test test-extender test-plugin \
        fmt-plugin lint-manifests \
        replay-plugin replay-extender all-plugin

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

# --- Tests -------------------------------------------------------------------

test: test-extender test-plugin

test-extender:
	cd bridge && uv run --group dev pytest -q

test-plugin:
	cd plugin && gofmt -l . && go vet ./... && go test ./...

fmt-plugin:
	cd plugin && gofmt -w .

# Parse every manifest with the Kubernetes YAML decoder (no cluster needed).
lint-manifests:
	cd plugin && go build -o /tmp/kaptain-parsecheck ./hack/parsecheck
	/tmp/kaptain-parsecheck
