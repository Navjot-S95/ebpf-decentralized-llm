#!/bin/bash
# deploy.sh — Build images and deploy to Docker Desktop Kubernetes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Building inference node image ==="
docker build -t ebpf-llm/inference:latest "$ROOT_DIR/inference"

echo "=== Building eBPF agent image ==="
docker build -t ebpf-llm/ebpf-agent:latest "$ROOT_DIR/ebpf"

echo "=== Applying Kubernetes manifests ==="
kubectl apply -f "$ROOT_DIR/k8s/namespace.yaml"
kubectl apply -f "$ROOT_DIR/k8s/node-a.yaml"
kubectl apply -f "$ROOT_DIR/k8s/node-b.yaml"
kubectl apply -f "$ROOT_DIR/k8s/ebpf-agent.yaml"

echo "=== Waiting for pods to be ready ==="
kubectl -n ebpf-llm wait --for=condition=ready pod -l app=inference-node --timeout=300s || true
kubectl -n ebpf-llm wait --for=condition=ready pod -l app=ebpf-agent --timeout=120s || true

echo "=== Pod status ==="
kubectl -n ebpf-llm get pods -o wide

echo ""
echo "Done! To test inference, run: ./scripts/test-inference.sh"
echo "To view eBPF metrics: curl http://localhost:9090/nodes"
