CLUSTER := kaptain
IMAGE   := kaptain-extender:dev

.PHONY: up build load deploy demo logs verify down all

# 1. Create the local multi-node cluster
up:
	kind create cluster --name $(CLUSTER) --config kind-cluster.yaml

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

# 5. Run the demo pods (ours + baseline)
demo:
	kubectl apply -f k8s/demo-workload.yaml

# Watch the extender being called (scores flow through here)
logs:
	kubectl -n kube-system logs -l app=scheduler-extender -f

# Check that our scheduler placed the pod
verify:
	@echo "== Demo pods and their node =="
	kubectl get pods -l app=demo -o wide
	@echo "\n== Who scheduled demo-ours? (must be ml-scheduler) =="
	kubectl get event --field-selector involvedObject.name=demo-ours | grep -i scheduled || true

down:
	kind delete cluster --name $(CLUSTER)

all: up build load deploy demo
