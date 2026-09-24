CLUSTER       := kaptain
PLUGIN_IMAGE  := kaptain-scheduler:dev
DECIDER_IMAGE := kaptain-decider:dev
K8S_VERSION   := v1.31.0

.PHONY: up down all-plugin \
        build-plugin load-plugin deploy-plugin demo-plugin logs-plugin verify-plugin \
        build-decider load-decider deploy-decider logs-decider metrics-decider \
        demo-default metrics-plugin check wait-ready \
        fmt-plugin lint-manifests

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

# The scheduler refuses to start without a decider, so both go up together.
deploy-plugin: deploy-decider
	kubectl apply -f k8s/scheduler-plugin-config.yaml
	kubectl apply -f k8s/scheduler-plugin.yaml

# Waits first: a pod submitted before the decider answers is placed by the fallback, and
# the run then measures the fallback rather than the policy. It is logged and counted, so
# it is visible -- but it is not what anyone running `make all-plugin` meant to see.
demo-plugin: wait-ready
	kubectl apply -f k8s/demo-workload-plugin.yaml

# The scheduler is not Ready until its own startup check has reached the decider through
# the same Service, so these two rollouts are the whole wait. A probe Pod of our own would
# land on a worker -- a measured node -- and leave its CPU in the kubelet summary that the
# first least-used decision reads.
wait-ready:
	kubectl -n kube-system rollout status deploy/kaptain-decider --timeout=120s
	kubectl -n kube-system rollout status deploy/kaptain-scheduler --timeout=180s

demo-default:
	kubectl apply -f k8s/demo-workload-default.yaml

logs-plugin:
	kubectl -n kube-system logs -l app=kaptain-scheduler -f

verify-plugin:
	@echo "== Demo pods and their node =="
	kubectl get pods -l integration=plugin -o wide
	@echo "\n== Who scheduled demo-plugin? (must be kaptain-scheduler) =="
	kubectl get event --field-selector involvedObject.name=demo-plugin | grep -i scheduled || true

all-plugin: up build-plugin load-plugin build-decider load-decider deploy-plugin demo-plugin

# --- The decider: where every policy runs -------------------------------------

build-decider:
	docker build -t $(DECIDER_IMAGE) bridge/

load-decider:
	kind load docker-image $(DECIDER_IMAGE) --name $(CLUSTER)

deploy-decider:
	kubectl apply -f k8s/decider.yaml

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

# --- Checks ------------------------------------------------------------------
# No automated test suite: this is a research prototype.

# gofmt -l prints the offenders and exits 0, so on its own it can never fail a check.
check: lint-manifests
	@cd plugin && out=$$(gofmt -l .); if [ -n "$$out" ]; then echo "gofmt: $$out"; exit 1; fi
	cd plugin && go vet ./...
	# The decider imports every strategy at startup, so one bad import breaks all of them.
	# This is the cheapest thing that catches it without a cluster and without a suite.
	cd bridge && uv run --frozen python -c "import app; \
	  from strategies import get_strategy; \
	  [get_strategy(n) for n in ('dummy-random','largest-cpu-capacity','least-allocated','least-used')]"

fmt-plugin:
	cd plugin && gofmt -w .

# Parse every manifest with the Kubernetes YAML decoder (no cluster needed).
lint-manifests:
	cd plugin && go build -o /tmp/kaptain-parsecheck ./hack/parsecheck
	/tmp/kaptain-parsecheck
