#!/bin/bash
# teardown.sh — Remove all resources from Kubernetes.
set -euo pipefail

echo "=== Deleting ebpf-llm namespace (all resources) ==="
kubectl delete namespace ebpf-llm --ignore-not-found

echo "=== Removing Docker images ==="
docker rmi ebpf-llm/inference:latest 2>/dev/null || true
docker rmi ebpf-llm/ebpf-agent:latest 2>/dev/null || true

echo "Done!"
