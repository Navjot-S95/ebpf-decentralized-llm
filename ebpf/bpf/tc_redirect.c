/* tc_redirect.c — TC (Traffic Control) eBPF program for decentralised
 * LLM inference routing.
 *
 * PATENT-RELEVANT FUNCTIONALITY:
 *   1. Inspects every egress packet on the inference pod's veth.
 *   2. If the packet targets an inference peer (gRPC port 50051),
 *      it marks the packet with a high-priority DSCP value so the
 *      kernel QoS scheduler treats inference traffic preferentially.
 *   3. Tracks per-destination traffic volume in traffic_metrics_map.
 *   4. If the target node's queue_depth exceeds a configurable threshold
 *      (stored in routing_config_map), the program can redirect the
 *      packet to an alternative, less-loaded node — achieving
 *      *decentralised, kernel-level load balancing* with zero userspace
 *      involvement per packet.
 *
 * Attach point: TC egress (cls_bpf / tcx).
 */

#include <linux/bpf.h>
#include <linux/pkt_cls.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/in.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

#include "maps.h"

#define INFERENCE_PORT  50051
#define ETH_P_IP        0x0800

/* Recompute IP header checksum after modifying TOS/DSCP.
 * Incremental update: RFC 1624.
 */
static __always_inline void update_ip_csum(struct iphdr *ip, __u8 old_tos, __u8 new_tos)
{
    __u32 csum = (~ip->check) & 0xFFFF;
    csum += (~old_tos) & 0xFFFF;
    csum += new_tos;
    csum = (csum & 0xFFFF) + (csum >> 16);
    csum = (csum & 0xFFFF) + (csum >> 16);
    ip->check = ~csum;
}

SEC("tc")
int tc_inference_redirect(struct __sk_buff *skb)
{
    /* ---- 0. Check if eBPF routing is enabled. ---- */
    __u32 key_enabled = CFG_ENABLED;
    __u64 *enabled = bpf_map_lookup_elem(&routing_config_map, &key_enabled);
    if (!enabled || *enabled == 0)
        return TC_ACT_OK;   /* pass-through */

    /* ---- 1. Parse Ethernet + IP + TCP headers. ---- */
    void *data     = (void *)(long)skb->data;
    void *data_end = (void *)(long)skb->data_end;

    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return TC_ACT_OK;
    if (eth->h_proto != bpf_htons(ETH_P_IP))
        return TC_ACT_OK;

    struct iphdr *ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end)
        return TC_ACT_OK;
    if (ip->protocol != IPPROTO_TCP)
        return TC_ACT_OK;

    if (ip->ihl < 5)
        return TC_ACT_OK;
    struct tcphdr *tcp = (void *)((char *)ip + (ip->ihl * 4));
    if ((void *)(tcp + 1) > data_end)
        return TC_ACT_OK;

    /* ---- 2. Filter: only act on inference gRPC traffic. ---- */
    __u16 dport = bpf_ntohs(tcp->dest);
    if (dport != INFERENCE_PORT)
        return TC_ACT_OK;

    /* ---- 3. Update traffic metrics for this destination. ---- */
    __u32 dst_ip = ip->daddr;
    struct traffic_metrics *tm = bpf_map_lookup_elem(&traffic_metrics_map, &dst_ip);
    if (tm) {
        __sync_fetch_and_add(&tm->packets, 1);
        __sync_fetch_and_add(&tm->bytes, skb->len);
        tm->last_seen_ns = bpf_ktime_get_ns();
    } else {
        struct traffic_metrics new_tm = {
            .packets = 1,
            .bytes   = skb->len,
            .last_seen_ns = bpf_ktime_get_ns(),
        };
        bpf_map_update_elem(&traffic_metrics_map, &dst_ip, &new_tm, BPF_ANY);
    }

    /* ---- 4. Set DSCP priority on inference packets. ---- */
    __u32 key_dscp = CFG_PRIORITY_DSCP;
    __u64 *dscp_val = bpf_map_lookup_elem(&routing_config_map, &key_dscp);
    if (dscp_val && *dscp_val > 0) {
        __u8 old_tos = ip->tos;
        __u8 new_tos = (old_tos & 0x03) | ((__u8)(*dscp_val) << 2);
        if (old_tos != new_tos) {
            update_ip_csum(ip, old_tos, new_tos);
            ip->tos = new_tos;
        }
    }

    /* ---- 5. Decentralised load-based redirect. ----
     *
     * The Go agent monitors node_state_map and pre-computes entries in
     * redirect_map: overloaded_ip → less_loaded_ip.
     * Single O(1) lookup — no loops, verifier-safe.
     *
     * PATENTABLE: kernel-level, per-packet, decentralised routing
     * decision with zero userspace involvement at packet time.
     */
    __u32 *new_dst = bpf_map_lookup_elem(&redirect_map, &dst_ip);
    if (new_dst && *new_dst != 0 && *new_dst != dst_ip) {
        __u32 old_dst = ip->daddr;
        ip->daddr = *new_dst;

        /* Incremental checksum update for daddr change (RFC 1624). */
        __u32 csum = (~ip->check) & 0xFFFF;
        csum += (~(old_dst & 0xFFFF)) & 0xFFFF;
        csum += (~(old_dst >> 16)) & 0xFFFF;
        csum += (*new_dst) & 0xFFFF;
        csum += (*new_dst) >> 16;
        csum = (csum & 0xFFFF) + (csum >> 16);
        csum = (csum & 0xFFFF) + (csum >> 16);
        ip->check = ~csum;

        bpf_printk("tc_redirect: redirected %x -> %x\n", old_dst, *new_dst);
    }

    return TC_ACT_OK;
}

char _license[] SEC("license") = "GPL";
