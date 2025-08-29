SHELL := /bin/bash
.ONESHELL:

include .env
export $(shell sed -n 's/=.*//p' .env 2>/dev/null)

PRED_IMAGE := ${PREDICTOR_IMAGE_NAME}:${PREDICTOR_IMAGE_TAG}

help:
	@grep -E '^[a-zA-Z_-]+:.*?##' Makefile | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}'

create-cluster: ## Create LKE cluster and write kubeconfig
	./provision.sh create

get-kubeconfig: ## Re-fetch kubeconfig for existing cluster
	./provision.sh kubeconfig

install-monitoring: ## Install kube-prometheus-stack
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts || true
	helm repo update
	helm upgrade --install monitoring prometheus-community/kube-prometheus-stack -n monitoring --create-namespace -f k8s/values-prom.yaml

.PHONY: install-keda
install-keda: ## Install/upgrade KEDA
	helm repo add kedacore https://kedacore.github.io/charts || true
	helm repo update
	helm upgrade --install keda kedacore/keda -n keda --create-namespace

wait-keda: ## Wait for KEDA operator and CRDs
	kubectl -n keda wait --for=condition=available deploy/keda-operator --timeout=180s
	kubectl get crd scaledobjects.keda.sh >/dev/null

namespace: ## Create app namespace
	kubectl create namespace demo || true

deploy-app: render ## Deploy demo app + service + ServiceMonitor
	kubectl apply -f k8s/.render/app.yaml

build-push-predictor: ## Build & push predictor image
	docker buildx build \
		--platform linux/amd64 \
		--progress=plain \
		-t $(PRED_IMAGE) \
		--provenance=false --sbom=false \
		--push predictor/

deploy-predictor: render ## Deploy predictor + Service + ServiceMonitor
	kubectl apply -f k8s/.render/predictor.yaml

deploy-scaledobject: render wait-keda ## Apply ScaledObject after CRDs exist
	kubectl apply -f k8s/.render/keda-scaledobject.yaml

do-all: create-cluster install-monitoring install-keda wait-keda namespace build-push-predictor deploy-predictor deploy-scaledobject deploy-app deploy-dashboard ## Do everything 

port-forward-prom: ## Port-forward Prometheus to localhost:9090
	kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090

.PHONY: deploy-dashboard
deploy-dashboard: ## Deploy the Grafana dashboard via ConfigMap
	kubectl apply -f k8s/grafana-dashboard.yaml
	kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80

cleanup: ## Delete cluster
	./provision.sh delete

.PHONY: render deploy-app deploy-predictor deploy-scaledobject
render: ## Render k8s manifests from .env into k8s/.render
	@mkdir -p k8s/.render
	@set -a; . ./.env; set +a; \
	envsubst < k8s/app.yaml > k8s/.render/app.yaml; \
	envsubst < k8s/predictor.yaml > k8s/.render/predictor.yaml; \
	envsubst < k8s/keda-scaledobject.yaml > k8s/.render/keda-scaledobject.yaml
	@echo "Rendered manifests -> k8s/.render"
