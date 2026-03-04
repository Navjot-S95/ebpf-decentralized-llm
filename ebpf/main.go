// Package main implements the eBPF userspace agent for decentralised
// LLM inference.  It loads TC and XDP programs, manages BPF maps,
// polls inference node health via gRPC, and exposes metrics.
package main

import (
	"context"
	"encoding/binary"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"encoding/json"
	"io"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"
	"github.com/cilium/ebpf/rlimit"
)

// ---------------------------------------------------------------------------
// BPF map value structs — must match bpf/maps.h layout exactly.
// ---------------------------------------------------------------------------

type NodeState struct {
	NodeID      uint32
	StageID     uint32
	QueueDepth  uint32
	CPUPercent  uint32
	MemPercent  uint32
	PacketsRx   uint64
	BytesRx     uint64
	AvgLatNs    uint64
	IPAddr      uint32
	Port        uint16
	_           uint16 // padding
}

type TrafficMetrics struct {
	Packets    uint64
	Bytes      uint64
	LastSeenNs uint64
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

type Config struct {
	Interface       string   // network interface to attach eBPF programs
	NodeAddrs       []string // gRPC host:port of inference nodes
	NodeStatusPort  int      // HTTP status port on inference nodes
	PollInterval    time.Duration
	MetricsPort     int
	EnableRedirect  bool
	PriorityDSCP    uint64
	RedirectThresh  uint64
}

func configFromEnv() Config {
	iface := envOr("EBPF_INTERFACE", "eth0")
	addrs := strings.Split(envOr("NODE_ADDRS", "inference-node-a:50051,inference-node-b:50051"), ",")
	poll, _ := time.ParseDuration(envOr("POLL_INTERVAL", "2s"))
	mport, _ := strconv.Atoi(envOr("METRICS_PORT", "9090"))
	sport, _ := strconv.Atoi(envOr("NODE_STATUS_PORT", "8080"))
	dscp, _ := strconv.ParseUint(envOr("PRIORITY_DSCP", "46"), 10, 64)
	thresh, _ := strconv.ParseUint(envOr("REDIRECT_THRESH", "5"), 10, 64)

	return Config{
		Interface:      iface,
		NodeAddrs:      addrs,
		NodeStatusPort: sport,
		PollInterval:   poll,
		MetricsPort:    mport,
		EnableRedirect: envOr("ENABLE_REDIRECT", "true") == "true",
		PriorityDSCP:   dscp,
		RedirectThresh: thresh,
	}
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	cfg := configFromEnv()
	log.Printf("eBPF agent starting — iface=%s nodes=%v", cfg.Interface, cfg.NodeAddrs)

	// Remove memlock rlimit for eBPF.
	if err := rlimit.RemoveMemlock(); err != nil {
		log.Fatalf("remove memlock: %v", err)
	}

	// ---- Load eBPF objects from compiled ELF. ----
	spec, err := ebpf.LoadCollectionSpec("bpf/tc_redirect.o")
	if err != nil {
		log.Fatalf("load tc spec: %v", err)
	}
	tcColl, err := ebpf.NewCollection(spec)
	if err != nil {
		log.Fatalf("new tc collection: %v", err)
	}
	defer tcColl.Close()

	specXDP, err := ebpf.LoadCollectionSpec("bpf/xdp_monitor.o")
	if err != nil {
		log.Fatalf("load xdp spec: %v", err)
	}
	xdpColl, err := ebpf.NewCollection(specXDP)
	if err != nil {
		log.Fatalf("new xdp collection: %v", err)
	}
	defer xdpColl.Close()

	// ---- Resolve network interface. ----
	iface, err := net.InterfaceByName(cfg.Interface)
	if err != nil {
		log.Fatalf("interface %s: %v", cfg.Interface, err)
	}

	// ---- Attach XDP program. ----
	xdpProg := xdpColl.Programs["xdp_inference_monitor"]
	if xdpProg == nil {
		log.Fatal("xdp program not found in collection")
	}
	xdpLink, err := link.AttachXDP(link.XDPOptions{
		Program:   xdpProg,
		Interface: iface.Index,
	})
	if err != nil {
		log.Fatalf("attach xdp: %v", err)
	}
	defer xdpLink.Close()
	log.Printf("XDP attached to %s (index %d)", cfg.Interface, iface.Index)

	// ---- Attach TC program. ----
	tcProg := tcColl.Programs["tc_inference_redirect"]
	if tcProg == nil {
		log.Fatal("tc program not found in collection")
	}
	tcLink, err := link.AttachTCX(link.TCXOptions{
		Program:   tcProg,
		Interface: iface.Index,
		Attach:    ebpf.AttachTCXEgress,
	})
	if err != nil {
		log.Fatalf("attach tc: %v", err)
	}
	defer tcLink.Close()
	log.Printf("TC attached to %s egress", cfg.Interface)

	// ---- Grab map references. ----
	nodeStateMap := tcColl.Maps["node_state_map"]
	trafficMap := tcColl.Maps["traffic_metrics_map"]
	routingCfg := tcColl.Maps["routing_config_map"]

	if nodeStateMap == nil || trafficMap == nil || routingCfg == nil {
		log.Fatal("required BPF maps not found")
	}

	// ---- Write initial routing config. ----
	writeRoutingConfig(routingCfg, cfg)

	// ---- Start metrics HTTP server. ----
	go serveMetrics(cfg.MetricsPort, nodeStateMap, trafficMap)

	// ---- Poll inference nodes and update BPF maps. ----
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go pollNodes(ctx, cfg, nodeStateMap)

	// Wait for signal.
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Println("Shutting down eBPF agent …")
}

// ---------------------------------------------------------------------------
// Routing config writer
// ---------------------------------------------------------------------------

func writeRoutingConfig(m *ebpf.Map, cfg Config) {
	var enabled uint64
	if cfg.EnableRedirect {
		enabled = 1
	}
	keys := []uint32{0, 1, 2} // CFG_ENABLED, CFG_PRIORITY_DSCP, CFG_REDIRECT_THRESH
	vals := []uint64{enabled, cfg.PriorityDSCP, cfg.RedirectThresh}
	for i, k := range keys {
		if err := m.Put(k, vals[i]); err != nil {
			log.Printf("write routing cfg key %d: %v", k, err)
		}
	}
	log.Printf("Routing config: enabled=%d dscp=%d thresh=%d", enabled, cfg.PriorityDSCP, cfg.RedirectThresh)
}

// ---------------------------------------------------------------------------
// Node poller — queries each inference node's HTTP /status endpoint and
// writes results into the BPF node_state_map.
// ---------------------------------------------------------------------------

type nodeStatusJSON struct {
	NodeID            string  `json:"node_id"`
	StageId           int32   `json:"stage_id"`
	LayerStart        int32   `json:"layer_start"`
	LayerEnd          int32   `json:"layer_end"`
	CpuPercent        float32 `json:"cpu_percent"`
	MemoryPercent     float32 `json:"memory_percent"`
	QueueDepth        int32   `json:"queue_depth"`
	AvgLatencyMs      float32 `json:"avg_latency_ms"`
	RequestsProcessed int64   `json:"requests_processed"`
}

func pollNodes(ctx context.Context, cfg Config, m *ebpf.Map) {
	client := &http.Client{Timeout: 3 * time.Second}
	ticker := time.NewTicker(cfg.PollInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			for idx, grpcAddr := range cfg.NodeAddrs {
				host := strings.Split(grpcAddr, ":")[0]
				url := fmt.Sprintf("http://%s:%d/status", host, cfg.NodeStatusPort)

				resp, err := client.Get(url)
				if err != nil {
					log.Printf("poll %s: %v", url, err)
					continue
				}
				body, _ := io.ReadAll(resp.Body)
				resp.Body.Close()

				var status nodeStatusJSON
				if err := json.Unmarshal(body, &status); err != nil {
					log.Printf("parse status %s: %v", url, err)
					continue
				}

				// Resolve IP.
				ips, err := net.LookupHost(host)
				var ipNum uint32
				if err == nil && len(ips) > 0 {
					ipNum = ipToUint32(net.ParseIP(ips[0]))
				}
				grpcPort, _ := strconv.Atoi(strings.Split(grpcAddr, ":")[1])

				ns := NodeState{
					NodeID:     uint32(idx),
					StageID:    uint32(status.StageId),
					QueueDepth: uint32(status.QueueDepth),
					CPUPercent: uint32(status.CpuPercent),
					MemPercent: uint32(status.MemoryPercent),
					AvgLatNs:   uint64(status.AvgLatencyMs * 1e6),
					IPAddr:     ipNum,
					Port:       uint16(grpcPort),
				}
				key := uint32(idx)
				if err := m.Put(key, unsafe.Slice((*byte)(unsafe.Pointer(&ns)), int(unsafe.Sizeof(ns)))); err != nil {
					log.Printf("update node_state_map[%d]: %v", idx, err)
				} else {
					log.Printf("node %s: stage=%d queue=%d cpu=%.0f%% lat=%.1fms",
						grpcAddr, status.StageId, status.QueueDepth,
						status.CpuPercent, status.AvgLatencyMs)
				}
			}
		}
	}
}

func ipToUint32(ip net.IP) uint32 {
	ip = ip.To4()
	if ip == nil {
		return 0
	}
	return binary.BigEndian.Uint32(ip)
}

// ---------------------------------------------------------------------------
// Metrics HTTP server — simple JSON endpoint for debugging / dashboards.
// ---------------------------------------------------------------------------

func serveMetrics(port int, nodeMap, trafficMap *ebpf.Map) {
	http.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"status":"ok","timestamp":"%s"}`, time.Now().UTC().Format(time.RFC3339))
	})

	http.HandleFunc("/nodes", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, "[")
		var key uint32
		var val NodeState
		iter := nodeMap.Iterate()
		first := true
		for iter.Next(&key, unsafe.Slice((*byte)(unsafe.Pointer(&val)), int(unsafe.Sizeof(val)))) {
			if !first {
				fmt.Fprint(w, ",")
			}
			first = false
			fmt.Fprintf(w, `{"node_id":%d,"stage_id":%d,"queue_depth":%d,"cpu":%d,"mem":%d,"avg_lat_ns":%d,"ip":"0x%08x"}`,
				val.NodeID, val.StageID, val.QueueDepth, val.CPUPercent, val.MemPercent, val.AvgLatNs, val.IPAddr)
		}
		fmt.Fprint(w, "]")
	})

	http.HandleFunc("/traffic", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, "[")
		var key uint32
		var val TrafficMetrics
		iter := trafficMap.Iterate()
		first := true
		for iter.Next(&key, unsafe.Slice((*byte)(unsafe.Pointer(&val)), int(unsafe.Sizeof(val)))) {
			if !first {
				fmt.Fprint(w, ",")
			}
			first = false
			ip := make(net.IP, 4)
			binary.BigEndian.PutUint32(ip, key)
			fmt.Fprintf(w, `{"ip":"%s","packets":%d,"bytes":%d,"last_seen_ns":%d}`,
				ip.String(), val.Packets, val.Bytes, val.LastSeenNs)
		}
		fmt.Fprint(w, "]")
	})

	addr := fmt.Sprintf(":%d", port)
	log.Printf("Metrics server on %s", addr)
	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatalf("metrics server: %v", err)
	}
}
