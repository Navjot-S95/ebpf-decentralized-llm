FROM python:3.11-slim

WORKDIR /app

# Install system deps for grpc compilation.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source.
COPY proto/ proto/
COPY pipeline.py .
COPY server.py .

# Generate gRPC stubs from proto.
RUN python -m grpc_tools.protoc \
    -Iproto \
    --python_out=. \
    --grpc_python_out=. \
    proto/inference.proto

# Pre-download the model so container startup is fast.
# Comment out to save image size during dev.
# RUN python -c "from transformers import AutoTokenizer, AutoModelForCausalLM; \
#     AutoTokenizer.from_pretrained('TinyLlama/TinyLlama-1.1B-Chat-v1.0'); \
#     AutoModelForCausalLM.from_pretrained('TinyLlama/TinyLlama-1.1B-Chat-v1.0')"

EXPOSE 50051

CMD ["python", "server.py"]
