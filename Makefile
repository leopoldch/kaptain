CLUSTER       := kaptain
PLUGIN_IMAGE  := kaptain-scheduler:dev
DECIDER_IMAGE := kaptain-decider:dev
K8S_VERSION   := v1.31.0

.PHONY: up down all-plugin \
        build-plugin load-plugin deploy-plugin demo-plugin logs-plugin verify-plugin \
        build-decider load-decider deploy-decider logs-decider metrics-decider \
        demo-default metrics-plugin test test-plugin test-decider \
        fmt-plugin lint-manifests replay-plugin replay-decider

# --- Cluster -----------------------------------------------------------------

# 1. Create the local multi-node cluster
up:
	kind create cluster --name $(CLUSTER) --config kind-cluster.yaml

down:
	kind delete cluster --name $(CLUSTER)

# --- The plugin: the integration ----------------------------------------------
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

demo-default:
	kubectl apply -f k8s/demo-workload-default.yaml

logs-plugin:
	kubectl -n kube-system logs -l app=kaptain-scheduler -f

verify-plugin:
	@echo "== Demo pods and their node =="
	kubectl get pods -l integration=plugin -o wide
	@echo "\n== Who scheduled demo-plugin? (must be kaptain-scheduler) =="
	kubectl get event --field-selector involvedObject.name=demo-plugin | grep -i scheduled || true

all-plugin: up build-plugin load-plugin deploy-plugin demo-plugin

# --- The decider: a policy the scheduler cannot host --------------------------
# Same plugin, same policy, running in another process. `deploy-decider` brings up both the
# Python service and the second scheduler that calls it.

build-decider:
	docker build -t $(DECIDER_IMAGE) bridge/

load-decider:
	kind load docker-image $(DECIDER_IMAGE) --name $(CLUSTER)

deploy-decider:
	kubectl apply -f k8s/decider.yaml
	kubectl apply -f k8s/scheduler-plugin-decider.yaml

logs-decider:
	kubectl -n kube-system logs -l app=kaptain-decider -f

# --- Metrics ------------------------------------------------------------------
# The scheduler image is distroless: no shell and no wget inside. Scrape from the host.

metrics-plugin:
	@kubectl -n kube-system port-forward deploy/kaptain-scheduler 10259:10259 >/dev/null 2>&1 & \
	forward=$$!; \
	trap "kill $$forward 2>/dev/null" EXIT; \
	until curl -sk https://127.0.0.1:10259/healthz >/dev/null 2>&1; do sleep 0.2; done; \
	curl -sk https://127.0.0.1:10259/metrics | grep kaptain_plugin

metrics-decider:
	@kubectl -n kube-system port-forward deploy/kaptain-decider 8888:8888 >/dev/null 2>&1 & \
	forward=$$!; \
	trap "kill $$forward 2>/dev/null" EXIT; \
	until curl -s http://127.0.0.1:8888/healthz >/dev/null 2>&1; do sleep 0.2; done; \
	curl -s http://127.0.0.1:8888/metrics | grep kaptain_decider

# --- Replay: the same snapshot through both processes -------------------------
# Replaying a fixture removes the cluster from the comparison and leaves the decision.

SNAPSHOT ?= testdata/snapshots/heterogeneous.json
STRATEGY ?= largest-cpu-capacity

replay-plugin:
	cd plugin && go run ./cmd/kaptain-replay -strategy $(STRATEGY) -snapshot ../$(SNAPSHOT) -repeat $(or $(REPEAT),1)

replay-decider:
	@python3 -c "import json,sys; print(json.dumps(json.load(open('$(SNAPSHOT)')).get('snapshot') or json.load(open('$(SNAPSHOT)'))))" \
		| curl -sS -X POST -H 'Content-Type: application/json' --data-binary @- \
		  http://127.0.0.1:8888/replay

# --- Tests -------------------------------------------------------------------

test: test-plugin test-decider

test-plugin:
	cd plugin && gofmt -l . && go vet ./... && go test ./...

test-decider:
	cd bridge && uv run --group dev pytest -q

fmt-plugin:
	cd plugin && gofmt -w .

# Parse every manifest with the Kubernetes YAML decoder (no cluster needed).
lint-manifests:
	cd plugin && go build -o /tmp/kaptain-parsecheck ./hack/parsecheck
	/tmp/kaptain-parsecheck
