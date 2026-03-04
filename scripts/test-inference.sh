#!/bin/bash
# test-inference.sh — Send a test inference request to Node A (stage 0).
set -euo pipefail

echo "=== Finding inference-node-a pod ==="
POD_A=$(kubectl -n ebpf-llm get pod -l app=inference-node,stage=0 -o jsonpath='{.items[0].metadata.name}')
echo "Pod: $POD_A"

echo "=== Port-forwarding Node A gRPC (50051 -> localhost:50051) ==="
kubectl -n ebpf-llm port-forward "$POD_A" 50051:50051 &
PF_PID=$!
sleep 2

echo "=== Sending inference request ==="
# Use grpcurl if available, otherwise a simple Python client.
if command -v grpcurl &>/dev/null; then
    grpcurl -plaintext \
        -d '{"request_id":"test-001","prompt":"The meaning of life is","max_tokens":1}' \
        -import-path "$(dirname "$0")/../inference/proto" \
        -proto inference.proto \
        localhost:50051 inference.InferenceNode/Infer
else
    echo "grpcurl not found, using Python client…"
    python3 - <<'PYEOF'
import grpc
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'inference'))

# Generate stubs inline if needed.
import subprocess
proto_dir = os.path.join(os.path.dirname(os.path.abspath(".")), "inference", "proto")
subprocess.run([
    sys.executable, "-m", "grpc_tools.protoc",
    f"-I{proto_dir}", f"--python_out=.", f"--grpc_python_out=.",
    os.path.join(proto_dir, "inference.proto"),
], check=True)

import inference_pb2
import inference_pb2_grpc

channel = grpc.insecure_channel("localhost:50051", options=[
    ("grpc.max_receive_message_length", 256*1024*1024),
])
stub = inference_pb2_grpc.InferenceNodeStub(channel)

resp = stub.Infer(inference_pb2.InferRequest(
    request_id="test-001",
    prompt="The meaning of life is",
    max_tokens=1,
))

print(f"\n=== Response ===")
print(f"Request ID:    {resp.request_id}")
print(f"Generated:     {resp.generated_text}")
print(f"Total latency: {resp.total_latency_ms:.1f} ms")
for sl in resp.stage_latencies:
    print(f"  Stage {sl.stage_id} ({sl.node_id}): {sl.latency_ms:.1f} ms")
PYEOF
fi

echo ""
echo "=== Checking eBPF metrics ==="
curl -s http://localhost:9090/nodes | python3 -m json.tool 2>/dev/null || echo "(eBPF agent metrics not available)"
curl -s http://localhost:9090/traffic | python3 -m json.tool 2>/dev/null || echo "(traffic metrics not available)"

# Clean up port-forward.
kill $PF_PID 2>/dev/null || true
echo ""
echo "Done!"
