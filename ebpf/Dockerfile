FROM golang:1.22-bookworm AS builder

# Install clang/llvm for compiling eBPF C programs and libbpf headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
    clang \
    llvm \
    libbpf-dev \
    linux-headers-generic \
    make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy source first (need all .go files for go mod tidy).
COPY . .

# Resolve dependencies.
RUN go mod tidy

# Compile eBPF C programs to BPF ELF objects.
RUN clang -O2 -g -target bpf \
    -I/usr/include/$(uname -m)-linux-gnu \
    -c bpf/xdp_monitor.c -o bpf/xdp_monitor.o

RUN clang -O2 -g -target bpf \
    -I/usr/include/$(uname -m)-linux-gnu \
    -c bpf/tc_redirect.c -o bpf/tc_redirect.o

# Build Go agent.
RUN CGO_ENABLED=0 go build -o ebpf-agent .

# --- Runtime image ---
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/ebpf-agent .
COPY --from=builder /app/bpf/*.o bpf/

EXPOSE 9090

ENTRYPOINT ["/app/ebpf-agent"]
