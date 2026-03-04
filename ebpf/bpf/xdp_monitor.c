/* xdp_monitor.c — XDP eBPF program for ingress monitoring of
 * decentralised LLM inference traffic.
 *
 * PATENT-RELEVANT FUNCTIONALITY:
 *   1. Runs at the earliest possible point in the network stack (XDP)
 *      for minimal-overhead per-packet inspection.
 *   2. Tracks incoming inference traffic per source IP.
 *   3. Updates traffic_metrics_map with real-time packet/byte counters.
 *   4. Enables the Go userspace agent to observe inter-node communication
 *      patterns without any application-layer instrumentation.
 *
 * Attach point: XDP (generic / native).
 */

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/in.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

#include "maps.h"

#define INFERENCE_PORT  50051
#define ETH_P_IP        0x0800

/* -------------------------------------------------------------------
 * Per-CPU packet counter for high-throughput, lock-free accounting.
 * Key: 0 (single entry)
 * Value: total inference packets seen on this CPU.
 * ---------------------------------------------------------------- */
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __type(key, __u32);
    __type(value, __u64);
    __uint(max_entries, 1);
} inference_pkt_counter SEC(".maps");

SEC("xdp")
int xdp_inference_monitor(struct xdp_md *ctx)
{
    void *data     = (void *)(long)ctx->data;
    void *data_end = (void *)(long)ctx->data_end;

    /* ---- 1. Parse Ethernet header. ---- */
    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return XDP_PASS;
    if (eth->h_proto != bpf_htons(ETH_P_IP))
        return XDP_PASS;

    /* ---- 2. Parse IP header. ---- */
    struct iphdr *ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end)
        return XDP_PASS;
    if (ip->protocol != IPPROTO_TCP)
        return XDP_PASS;

    /* ---- 3. Parse TCP header. ---- */
    struct tcphdr *tcp = (void *)ip + (ip->ihl * 4);
    if ((void *)(tcp + 1) > data_end)
        return XDP_PASS;

    /* ---- 4. Filter: only track inference traffic. ---- */
    __u16 sport = bpf_ntohs(tcp->source);
    __u16 dport = bpf_ntohs(tcp->dest);

    int is_inference = (sport == INFERENCE_PORT || dport == INFERENCE_PORT);
    if (!is_inference)
        return XDP_PASS;

    /* ---- 5. Update per-source traffic metrics. ---- */
    __u32 src_ip = ip->saddr;
    __u32 pkt_len = data_end - data;

    struct traffic_metrics *tm = bpf_map_lookup_elem(&traffic_metrics_map, &src_ip);
    if (tm) {
        __sync_fetch_and_add(&tm->packets, 1);
        __sync_fetch_and_add(&tm->bytes, pkt_len);
        tm->last_seen_ns = bpf_ktime_get_ns();
    } else {
        struct traffic_metrics new_tm = {
            .packets = 1,
            .bytes   = pkt_len,
            .last_seen_ns = bpf_ktime_get_ns(),
        };
        bpf_map_update_elem(&traffic_metrics_map, &src_ip, &new_tm, BPF_ANY);
    }

    /* ---- 6. Bump per-CPU inference packet counter. ---- */
    __u32 zero = 0;
    __u64 *counter = bpf_map_lookup_elem(&inference_pkt_counter, &zero);
    if (counter)
        *counter += 1;

    bpf_printk("xdp_monitor: inference pkt from %x, %d bytes\n", src_ip, pkt_len);

    return XDP_PASS;
}

char _license[] SEC("license") = "GPL";
