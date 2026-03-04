"""Pipeline-parallel model split for TinyLlama across multiple nodes.

Each node loads only its assigned slice of transformer layers and runs
forward passes on that slice.  Intermediate hidden-state tensors are
shipped between nodes via gRPC.
"""

import os
import time
import logging
from typing import Optional, Tuple

import torch
import numpy as np
from transformers import AutoTokenizer, AutoModelForCausalLM, AutoConfig

logger = logging.getLogger(__name__)

MODEL_NAME = os.getenv("MODEL_NAME", "gpt2")


class PipelineStage:
    """Holds a contiguous slice of transformer layers."""

    def __init__(
        self,
        stage_id: int,
        total_stages: int,
        device: str = "cpu",
    ):
        self.stage_id = stage_id
        self.total_stages = total_stages
        self.device = device

        self.tokenizer: Optional[AutoTokenizer] = None
        self.config = None
        self.embed_tokens = None
        self.embed_pos = None        # GPT-2 wpe; None for rotary models
        self.layers = None
        self.norm = None
        self.lm_head = None

        self.layer_start = 0
        self.layer_end = 0
        self.total_layers = 0

        self._load_model_slice()

    # ------------------------------------------------------------------
    def _load_model_slice(self):
        """Download the full model, keep only our layer slice, free the rest."""
        logger.info("Loading model config for %s …", MODEL_NAME)
        self.config = AutoConfig.from_pretrained(MODEL_NAME)
        self.total_layers = self.config.num_hidden_layers

        # Compute which layers this stage owns.
        layers_per_stage = self.total_layers // self.total_stages
        self.layer_start = self.stage_id * layers_per_stage
        self.layer_end = (
            self.total_layers
            if self.stage_id == self.total_stages - 1
            else (self.stage_id + 1) * layers_per_stage
        )
        logger.info(
            "Stage %d: layers [%d, %d) of %d",
            self.stage_id, self.layer_start, self.layer_end, self.total_layers,
        )

        # Load the full model (weights are lazy-downloaded).
        logger.info("Downloading model weights …")
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_NAME,
            dtype=torch.float32,
            low_cpu_mem_usage=True,
        )
        model.eval()

        # -- keep only what we need --
        # Support both GPT-2 style (transformer.h) and Llama style (model.layers).
        if hasattr(model, "transformer"):
            inner = model.transformer      # GPT-2
            all_layers = inner.h
            embed = inner.wte
            ln_final = inner.ln_f
            lm_head = model.lm_head
        else:
            inner = model.model            # Llama / TinyLlama
            all_layers = inner.layers
            embed = inner.embed_tokens
            ln_final = inner.norm
            lm_head = model.lm_head

        if self.stage_id == 0:
            self.embed_tokens = embed.to(self.device)
            # GPT-2 also has positional embeddings.
            if hasattr(inner, "wpe"):
                self.embed_pos = inner.wpe.to(self.device)
            self.tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
            if self.tokenizer.pad_token is None:
                self.tokenizer.pad_token = self.tokenizer.eos_token

        # Keep our slice of transformer layers.
        self.layers = torch.nn.ModuleList(
            [all_layers[i].to(self.device) for i in range(self.layer_start, self.layer_end)]
        )

        if self.stage_id == self.total_stages - 1:
            self.norm = ln_final.to(self.device)
            self.lm_head = lm_head.to(self.device)
            if self.tokenizer is None:
                self.tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
                if self.tokenizer.pad_token is None:
                    self.tokenizer.pad_token = self.tokenizer.eos_token

        # Free the full model.
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        logger.info("Stage %d ready – holding %d layers.", self.stage_id, len(self.layers))

    # ------------------------------------------------------------------
    def encode_prompt(self, prompt: str) -> torch.Tensor:
        """Tokenise and embed a prompt (token + position). Only valid on stage 0."""
        assert self.embed_tokens is not None, "encode_prompt called on non-first stage"
        tokens = self.tokenizer(prompt, return_tensors="pt").input_ids.to(self.device)
        hidden = self.embed_tokens(tokens)
        if self.embed_pos is not None:
            seq_len = tokens.shape[1]
            pos_ids = torch.arange(seq_len, device=self.device).unsqueeze(0)
            hidden = hidden + self.embed_pos(pos_ids)
        return hidden

    # ------------------------------------------------------------------
    @torch.no_grad()
    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        """Run our layer slice and return the resulting hidden states."""
        for layer in self.layers:
            out = layer(hidden_states)
            # Both GPT-2 and Llama return a tuple; first element is hidden states.
            hidden_states = out[0]
        return hidden_states

    # ------------------------------------------------------------------
    @torch.no_grad()
    def decode_hidden(self, hidden_states: torch.Tensor, max_tokens: int = 1) -> str:
        """Apply final norm + lm_head and decode tokens.  Only valid on last stage."""
        assert self.norm is not None, "decode_hidden called on non-last stage"
        hidden_states = self.norm(hidden_states)
        logits = self.lm_head(hidden_states)
        # Greedy – pick the last token.
        next_token_id = torch.argmax(logits[:, -1, :], dim=-1)
        return self.tokenizer.decode(next_token_id, skip_special_tokens=True)

    # ------------------------------------------------------------------
    # Serialisation helpers for gRPC transport.
    # ------------------------------------------------------------------
    @staticmethod
    def tensor_to_bytes(t: torch.Tensor) -> Tuple[bytes, list]:
        arr = t.cpu().numpy().astype(np.float32)
        return arr.tobytes(), list(arr.shape)

    @staticmethod
    def bytes_to_tensor(data: bytes, shape: list, device: str = "cpu") -> torch.Tensor:
        arr = np.frombuffer(data, dtype=np.float32).reshape(shape)
        return torch.from_numpy(arr.copy()).to(device)
