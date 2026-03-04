# eBPF Decentralized LLM Inference

A proof-of-concept for **kernel-level coordination of distributed LLM inference** using eBPF — without a central orchestrator.

Instead of a userspace load balancer or service mesh, this system uses Linux TC hooks and BPF maps to route, prioritize, and failover inference traffic directly in the kernel.

## What This Does

Splits a language model (GPT-2) across two Kubernetes pods in pipeline-parallel fashion. An eBPF DaemonSet running on the host network handles:

- **Traffic prioritization** — DSCP marking of gRPC inference traffic (port 50051) so it's served before background traffic
- **Load-aware redirect** — TC egress hook reads node load from a BPF map and rewrites packet destination in-kernel (O(1) BPF hashmap lookup, no userspace involvement)
- **Zero-overhead monitoring** — XDP ingress hook tracks per-node packet counters and latency without copying packets to userspace
- **Decentralized state** — BPF maps serve as the shared coordination state across nodes, polled and updated by a Go agent every 2 seconds

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Kubernetes Cluster                  │
│                                                      │
│  ┌──────────────┐   gRPC activations  ┌───────────┐ │
│  │ inference-a  │ ──────────────────► │inference-b│ │
│  │ (layers 0-5) │                     │(layers6-11│ │
│  └──────────────┘                     └───────────┘ │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │              eBPF DaemonSet (host network)       │ │
│  │                                                  │ │
│  │  XDP hook    ─── ingress monitoring              │ │
│  │  TC hook     ─── egress: DSCP mark + redirect    │ │
│  │  BPF maps    ─── node_state, redirect_map,       │ │
│  │                  traffic_metrics, routing_config │ │
│  │  Go agent    ─── polls node health → writes maps │ │
│  │  HTTP :9090  ─── exposes metrics                 │ │
│  └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

## Novel Aspects

1. **BPF maps as decentralized coordination state** — no central load balancer, no service mesh proxy. The kernel IS the coordinator.
2. **O(1) kernel-space failover** — `redirect_map` rewrites packet destination in TC egress hook. When a node is overloaded, traffic redirects without touching userspace.
3. **Zero-copy observability** — XDP hook monitors all inference traffic with near-zero overhead.
4. **Separation of planes** — control plane (Go agent, health polling) is fully decoupled from data plane (eBPF). Data plane never blocks on control plane decisions.

## Stack

| Component | Technology |
|-----------|-----------|
| Inference nodes | Python 3.11, PyTorch, HuggingFace Transformers |
| Inter-node comms | gRPC (protobuf) |
| eBPF programs | C (clang -target bpf) |
| eBPF userspace | Go 1.22 + [cilium/ebpf](https://github.com/cilium/ebpf) |
| Orchestration | Kubernetes (Docker Desktop) |
| Model | GPT-2 (split: layers 0-5 / 6-11) |

## Quick Start

**Prerequisites:** Docker Desktop with Kubernetes enabled, `kubectl` configured.

```bash
# Deploy everything
./scripts/deploy.sh

# Send a test prompt
./scripts/test-inference.sh

# Run latency benchmark
./scripts/benchmark.sh 10

# View eBPF node metrics
curl http://localhost:30090/nodes

# Tear down
./scripts/teardown.sh
```

## Key Files

```
ebpf/bpf/tc_redirect.c   — TC egress hook: DSCP marking + overload redirect
ebpf/bpf/xdp_monitor.c   — XDP ingress hook: traffic monitoring
ebpf/bpf/maps.h           — BPF map definitions (node_state, redirect_map, etc.)
ebpf/main.go              — Go agent: loads eBPF, polls node health, metrics server
inference/pipeline.py     — Model layer splitting + tensor serialization
inference/server.py       — gRPC server + HTTP /status endpoint
k8s/                      — Kubernetes manifests (namespace, deployments, DaemonSet)
```

## BPF Maps

| Map | Type | Purpose |
|-----|------|---------|
| `node_state_map` | HASH | Per-node load, queue depth, latency |
| `redirect_map` | HASH | Overloaded IP → redirect IP (O(1) failover) |
| `traffic_metrics_map` | HASH | Per-source packet/byte counters |
| `routing_config_map` | ARRAY | Global config: DSCP value, redirect threshold |

## How the Redirect Works

```c
// TC egress hook — runs in kernel for every outbound packet
struct redirect_target *target = bpf_map_lookup_elem(&redirect_map, &ip->daddr);
if (target) {
    // rewrite destination IP in-kernel, update checksum
    // zero userspace involvement, ~300ns decision time
}
```

## Related Work

- [cilium/ebpf](https://github.com/cilium/ebpf) — Go library for eBPF used in this project
- [ProfInfer](https://arxiv.org/abs/2601.20755) — eBPF-based LLM inference profiling
