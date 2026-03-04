"""gRPC inference server — each instance runs one pipeline stage.

Environment variables:
  NODE_ID        – unique name, e.g. "node-a"
  STAGE_ID       – integer stage index (0-based)
  TOTAL_STAGES   – total number of pipeline stages
  NEXT_NODE_ADDR – gRPC address of the next stage (empty for last stage)
  GRPC_PORT      – port to listen on (default 50051)
  MODEL_NAME     – HuggingFace model id (default TinyLlama)
"""

import os
import sys
import time
import logging
import threading
from concurrent import futures

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import grpc
import psutil

# -- generated gRPC stubs (created at container build time) --
sys.path.insert(0, os.path.dirname(__file__))
import inference_pb2
import inference_pb2_grpc

from pipeline import PipelineStage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("inference-server")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
NODE_ID = os.getenv("NODE_ID", "node-0")
STAGE_ID = int(os.getenv("STAGE_ID", "0"))
TOTAL_STAGES = int(os.getenv("TOTAL_STAGES", "2"))
NEXT_NODE_ADDR = os.getenv("NEXT_NODE_ADDR", "")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50051"))


class InferenceServicer(inference_pb2_grpc.InferenceNodeServicer):
    def __init__(self):
        logger.info("Initialising pipeline stage %d / %d …", STAGE_ID, TOTAL_STAGES)
        self.stage = PipelineStage(
            stage_id=STAGE_ID,
            total_stages=TOTAL_STAGES,
        )
        # Simple metrics.
        self._lock = threading.Lock()
        self._requests_processed = 0
        self._total_latency = 0.0
        self._queue_depth = 0

        # Lazy gRPC channel to next node.
        self._next_stub = None
        if NEXT_NODE_ADDR:
            logger.info("Next stage at %s", NEXT_NODE_ADDR)

    # ------------------------------------------------------------------
    def _get_next_stub(self):
        if self._next_stub is None and NEXT_NODE_ADDR:
            chan = grpc.insecure_channel(
                NEXT_NODE_ADDR,
                options=[
                    ("grpc.max_send_message_length", 256 * 1024 * 1024),
                    ("grpc.max_receive_message_length", 256 * 1024 * 1024),
                ],
            )
            self._next_stub = inference_pb2_grpc.InferenceNodeStub(chan)
        return self._next_stub

    # ------------------------------------------------------------------
    # RPC: Infer  (entry point – only called on stage 0)
    # ------------------------------------------------------------------
    def Infer(self, request, context):
        logger.info("[%s] Infer request: %.60s…", request.request_id, request.prompt)
        t0 = time.time()

        with self._lock:
            self._queue_depth += 1

        try:
            # 1. Embed the prompt.
            hidden = self.stage.encode_prompt(request.prompt)

            # 2. Run our layers.
            t_stage = time.time()
            hidden = self.stage.forward(hidden)
            stage_lat = (time.time() - t_stage) * 1000

            stage_latencies = [
                inference_pb2.StageLatency(
                    stage_id=STAGE_ID, node_id=NODE_ID, latency_ms=stage_lat,
                )
            ]

            # 3. Forward to next stage or decode.
            if NEXT_NODE_ADDR:
                tensor_bytes, tensor_shape = PipelineStage.tensor_to_bytes(hidden)
                fwd = inference_pb2.ActivationData(
                    request_id=request.request_id,
                    source_stage=STAGE_ID,
                    target_stage=STAGE_ID + 1,
                    tensor_data=tensor_bytes,
                    tensor_shape=tensor_shape,
                    prompt=request.prompt,
                    max_tokens=request.max_tokens,
                )
                stub = self._get_next_stub()
                resp = stub.ForwardActivations(fwd)
                generated = resp.generated_text
                # Collect downstream latency.
                # (downstream sends its own stage latency in resp.latency_ms)
                stage_latencies.append(
                    inference_pb2.StageLatency(
                        stage_id=STAGE_ID + 1,
                        node_id="downstream",
                        latency_ms=resp.latency_ms,
                    )
                )
            else:
                generated = self.stage.decode_hidden(hidden, request.max_tokens)

            total_ms = (time.time() - t0) * 1000
            with self._lock:
                self._requests_processed += 1
                self._total_latency += total_ms
                self._queue_depth -= 1

            return inference_pb2.InferResponse(
                request_id=request.request_id,
                generated_text=generated,
                total_latency_ms=total_ms,
                stage_latencies=stage_latencies,
            )
        except Exception as exc:
            logger.exception("Infer failed")
            with self._lock:
                self._queue_depth -= 1
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return inference_pb2.InferResponse()

    # ------------------------------------------------------------------
    # RPC: ForwardActivations  (called by upstream stage)
    # ------------------------------------------------------------------
    def ForwardActivations(self, request, context):
        logger.info(
            "[%s] ForwardActivations from stage %d",
            request.request_id, request.source_stage,
        )
        t0 = time.time()

        with self._lock:
            self._queue_depth += 1

        try:
            hidden = PipelineStage.bytes_to_tensor(
                request.tensor_data, list(request.tensor_shape),
            )
            hidden = self.stage.forward(hidden)

            if NEXT_NODE_ADDR:
                # Not the last stage — forward again.
                tensor_bytes, tensor_shape = PipelineStage.tensor_to_bytes(hidden)
                fwd = inference_pb2.ActivationData(
                    request_id=request.request_id,
                    source_stage=STAGE_ID,
                    target_stage=STAGE_ID + 1,
                    tensor_data=tensor_bytes,
                    tensor_shape=tensor_shape,
                    prompt=request.prompt,
                    max_tokens=request.max_tokens,
                )
                stub = self._get_next_stub()
                resp = stub.ForwardActivations(fwd)
                generated = resp.generated_text
            else:
                # Last stage — decode.
                generated = self.stage.decode_hidden(hidden, request.max_tokens)

            lat_ms = (time.time() - t0) * 1000

            with self._lock:
                self._requests_processed += 1
                self._total_latency += lat_ms
                self._queue_depth -= 1

            return inference_pb2.ActivationResponse(
                request_id=request.request_id,
                accepted=True,
                generated_text=generated,
                latency_ms=lat_ms,
            )
        except Exception as exc:
            logger.exception("ForwardActivations failed")
            with self._lock:
                self._queue_depth -= 1
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return inference_pb2.ActivationResponse(accepted=False)

    # ------------------------------------------------------------------
    # RPC: GetNodeStatus
    # ------------------------------------------------------------------
    def GetNodeStatus(self, request, context):
        with self._lock:
            avg_lat = (
                self._total_latency / self._requests_processed
                if self._requests_processed > 0
                else 0.0
            )
            return inference_pb2.NodeStatus(
                node_id=NODE_ID,
                stage_id=STAGE_ID,
                layer_start=self.stage.layer_start,
                layer_end=self.stage.layer_end,
                cpu_percent=psutil.cpu_percent(),
                memory_percent=psutil.virtual_memory().percent,
                queue_depth=self._queue_depth,
                avg_latency_ms=avg_lat,
                requests_processed=self._requests_processed,
            )


HTTP_STATUS_PORT = int(os.getenv("HTTP_STATUS_PORT", "8080"))

_servicer_ref: "InferenceServicer | None" = None


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/status":
            self.send_response(404)
            self.end_headers()
            return
        svc = _servicer_ref
        if svc is None:
            payload = {"error": "not ready"}
        else:
            with svc._lock:
                avg_lat = (
                    svc._total_latency / svc._requests_processed
                    if svc._requests_processed > 0
                    else 0.0
                )
            payload = {
                "node_id": NODE_ID,
                "stage_id": STAGE_ID,
                "layer_start": svc.stage.layer_start,
                "layer_end": svc.stage.layer_end,
                "cpu_percent": psutil.cpu_percent(),
                "memory_percent": psutil.virtual_memory().percent,
                "queue_depth": svc._queue_depth,
                "avg_latency_ms": avg_lat,
                "requests_processed": svc._requests_processed,
            }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress access logs


def serve():
    global _servicer_ref
    servicer = InferenceServicer()
    _servicer_ref = servicer

    # HTTP status server (for eBPF agent polling).
    http_server = HTTPServer(("0.0.0.0", HTTP_STATUS_PORT), StatusHandler)
    threading.Thread(target=http_server.serve_forever, daemon=True).start()
    logger.info("HTTP status on :%d", HTTP_STATUS_PORT)

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_send_message_length", 256 * 1024 * 1024),
            ("grpc.max_receive_message_length", 256 * 1024 * 1024),
        ],
    )
    inference_pb2_grpc.add_InferenceNodeServicer_to_server(servicer, server)
    addr = f"0.0.0.0:{GRPC_PORT}"
    server.add_insecure_port(addr)
    logger.info("Serving on %s (stage %d, node %s)", addr, STAGE_ID, NODE_ID)
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
