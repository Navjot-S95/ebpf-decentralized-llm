/* maps.h — Shared BPF map definitions for the decentralised LLM inference
 * eBPF programs.
 *
 * These maps are the core of the "no central coordinator" design:
 *   • Each node's eBPF program writes its own metrics into the maps.
 *   • The Go userspace agent reads them to expose observability.
 *   • TC / XDP programs read peer entries to make local routing decisions.
 */

#ifndef __MAPS_H__
#define __MAPS_H__

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

/* -------------------------------------------------------------------
 * NODE_STATE_MAP
 * Key:   node index (u32, 0–255)
 * Value: struct node_state
 *
 * Each node keeps its own entry up-to-date.  TC/XDP programs on other
 * nodes read the map to decide whether to redirect traffic.
 * ---------------------------------------------------------------- */
struct node_state {
    __u32 node_id;           /* matches k8s pod index          */
    __u32 stage_id;          /* pipeline stage this node runs  */
    __u32 queue_depth;       /* current pending requests       */
    __u32 cpu_percent;       /* 0-100                          */
    __u32 mem_percent;       /* 0-100                          */
    __u64 packets_rx;        /* total packets received         */
    __u64 bytes_rx;          /* total bytes received           */
    __u64 avg_latency_ns;    /* rolling average inference lat  */
    __u32 ip_addr;           /* node's IP in network order     */
    __u16 port;              /* gRPC port in network order     */
    __u16 _pad;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __type(key, __u32);
    __type(value, struct node_state);
    __uint(max_entries, 256);
} node_state_map SEC(".maps");


/* -------------------------------------------------------------------
 * TRAFFIC_METRICS_MAP
 * Key:   destination IP (u32, network order)
 * Value: struct traffic_metrics
 *
 * Updated by TC/XDP on every packet to track inter-node bandwidth.
 * ---------------------------------------------------------------- */
struct traffic_metrics {
    __u64 packets;
    __u64 bytes;
    __u64 last_seen_ns;      /* bpf_ktime_get_ns() timestamp  */
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __type(key, __u32);
    __type(value, struct traffic_metrics);
    __uint(max_entries, 1024);
} traffic_metrics_map SEC(".maps");


/* -------------------------------------------------------------------
 * INFERENCE_LATENCY_HIST
 * Key:   latency bucket index (u32, 0-31)
 *        bucket boundaries: 0=<1ms, 1=1-2ms, … 20=1-2s, …
 * Value: count (u64)
 *
 * Populated by userspace (Go agent) based on gRPC response times,
 * readable by eBPF programs for adaptive routing.
 * ---------------------------------------------------------------- */
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __type(key, __u32);
    __type(value, __u64);
    __uint(max_entries, 32);
} inference_latency_hist SEC(".maps");


/* -------------------------------------------------------------------
 * ROUTING_CONFIG_MAP
 * Key:   config key id (u32)
 * Value: u64
 *
 * Userspace-controlled knobs that influence eBPF routing behaviour.
 * Keys:
 *   0 = ENABLED          (0/1)
 *   1 = PRIORITY_DSCP    (DSCP value to set on inference pkts)
 *   2 = REDIRECT_THRESH  (queue depth above which to redirect)
 * ---------------------------------------------------------------- */
#define CFG_ENABLED          0
#define CFG_PRIORITY_DSCP    1
#define CFG_REDIRECT_THRESH  2

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __type(key, __u32);
    __type(value, __u64);
    __uint(max_entries, 16);
} routing_config_map SEC(".maps");

/* -------------------------------------------------------------------
 * REDIRECT_MAP
 * Key:   original destination IP (u32, network order)
 * Value: redirect target IP (u32, network order)
 *
 * Pre-computed by the Go userspace agent.  When a node is overloaded,
 * the agent writes the IP of a less-loaded peer here.  The TC program
 * does a single O(1) lookup — no loops, verifier-safe.
 * ---------------------------------------------------------------- */
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __type(key, __u32);
    __type(value, __u32);
    __uint(max_entries, 256);
} redirect_map SEC(".maps");

#endif /* __MAPS_H__ */
