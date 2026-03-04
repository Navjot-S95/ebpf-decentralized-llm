#!/bin/bash
# benchmark.sh — Run a simple latency benchmark: N sequential requests.
set -euo pipefail

NUM_REQUESTS=${1:-10}
PROMPT=${2:-"Explain quantum computing in one sentence"}

echo "=== Benchmark: $NUM_REQUESTS requests ==="

# Port-forward.
POD_A=$(kubectl -n ebpf-llm get pod -l app=inference-node,stage=0 -o jsonpath='{.items[0].metadata.name}')
kubectl -n ebpf-llm port-forward "$POD_A" 50051:50051 &
PF_PID=$!
sleep 2

python3 - "$NUM_REQUESTS" "$PROMPT" <<'PYEOF'
import grpc, sys, time, os, subprocess, statistics

n = int(sys.argv[1])
prompt = sys.argv[2]

# Build stubs.
proto_dir = os.path.join(os.path.dirname(os.path.abspath(".")),
                         "ebpf-decentralized-llm", "inference", "proto")
if os.path.exists(proto_dir):
    subprocess.run([
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I{proto_dir}", "--python_out=.", "--grpc_python_out=.",
        os.path.join(proto_dir, "inference.proto"),
    ], check=True)

import inference_pb2, inference_pb2_grpc

channel = grpc.insecure_channel("localhost:50051", options=[
    ("grpc.max_receive_message_length", 256*1024*1024),
])
stub = inference_pb2_grpc.InferenceNodeStub(channel)

latencies = []
for i in range(n):
    t0 = time.time()
    resp = stub.Infer(inference_pb2.InferRequest(
        request_id=f"bench-{i:04d}",
        prompt=prompt,
        max_tokens=1,
    ))
    lat = (time.time() - t0) * 1000
    latencies.append(lat)
    print(f"  [{i+1}/{n}] {lat:.1f} ms  =>  {resp.generated_text!r}")

print(f"\n=== Results ({n} requests) ===")
print(f"  Mean:   {statistics.mean(latencies):.1f} ms")
print(f"  Median: {statistics.median(latencies):.1f} ms")
print(f"  Stdev:  {statistics.stdev(latencies):.1f} ms" if n > 1 else "")
print(f"  Min:    {min(latencies):.1f} ms")
print(f"  Max:    {max(latencies):.1f} ms")
PYEOF

kill $PF_PID 2>/dev/null || true
echo "Done!"
