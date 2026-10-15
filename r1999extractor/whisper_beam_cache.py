"""Optional Whisper model with checked reuse of redundant CPU generation work."""

from weakref import ref

import torch
from transformers import WhisperForConditionalGeneration
from transformers.cache_utils import DynamicCache, DynamicLayer, EncoderDecoderCache
from transformers.generation.logits_process import SuppressTokensLogitsProcessor
from transformers.pytorch_utils import isin_mps_friendly


class _CpuSuppressTokensLogitsProcessor(SuppressTokensLogitsProcessor):
    """Reuse only the unchanged native CPU suppression mask."""

    def __init__(self, processor):
        self.suppress_tokens = processor.suppress_tokens
        self._mask_snapshot = None

    def __call__(self, input_ids, scores):
        tokens = self.suppress_tokens
        if (
            type(scores) is not torch.Tensor
            or scores.device.type != "cpu"
            or scores.layout != torch.strided
            or scores.ndim != 2
            or type(tokens) is not torch.Tensor
            or tokens.device.type != "cpu"
            or tokens.layout != torch.strided
            or tokens.dtype != torch.int64
            or tokens.ndim != 1
        ):
            return super().__call__(input_ids, scores)
        snapshot = self._mask_snapshot
        if (
            snapshot is None
            or snapshot[0] != scores.shape[-1]
            or not torch.equal(snapshot[1], tokens)
        ):
            mask = isin_mps_friendly(torch.arange(scores.shape[-1], device=scores.device), tokens)
            snapshot = (scores.shape[-1], tokens.clone(), mask)
            self._mask_snapshot = snapshot
        return torch.where(snapshot[2], -float("inf"), scores)


class CpuWhisperForConditionalGeneration(WhisperForConditionalGeneration):
    """Keep normal generation while avoiding proven redundant CPU work."""

    def _retrieve_logit_processors(
        self, generation_config, logits_processor, begin_index, num_beams, device
    ):
        supplied = tuple(logits_processor or ())
        processors = super()._retrieve_logit_processors(
            generation_config, logits_processor, begin_index, num_beams, device
        )
        for index, processor in enumerate(processors or ()):
            if (
                type(processor) is SuppressTokensLogitsProcessor
                and processor.suppress_tokens.device.type == "cpu"
                and all(processor is not item for item in supplied)
            ):
                processors[index] = _CpuSuppressTokensLogitsProcessor(processor)
        return processors

    def _reorder_cache(self, cache, beam_idx):
        if self._can_reuse_cross_cache(cache, beam_idx):
            cache.self_attention_cache.reorder_cache(beam_idx)
        else:
            self._cross_beam_snapshot = None
            cache.reorder_cache(beam_idx)
        return cache

    def _can_reuse_cross_cache(self, cache, beam_idx):
        if (
            type(cache) is not EncoderDecoderCache
            or type(cache.self_attention_cache) is not DynamicCache
            or type(cache.cross_attention_cache) is not DynamicCache
            or beam_idx.device.type != "cpu"
            or beam_idx.dtype not in (torch.int32, torch.int64)
            or beam_idx.ndim != 1
            or beam_idx.numel() < 2
            or beam_idx.min().item() < 0
            or beam_idx.max().item() >= beam_idx.numel()
        ):
            return False
        layers = cache.cross_attention_cache.layers
        if not layers or any(type(layer) is not DynamicLayer for layer in layers):
            return False
        tensors = tuple(tensor for layer in layers for tensor in (layer.keys, layer.values))
        if any(
            not isinstance(tensor, torch.Tensor)
            or tensor.device.type != "cpu"
            or tensor.dtype != torch.float32
            or tensor.layout != torch.strided
            or tensor.ndim != 4
            or tensor.shape[0] != beam_idx.numel()
            or not tensor.is_contiguous()
            for tensor in tensors
        ):
            return False
        try:
            versions = tuple(tensor._version for tensor in tensors)
        except RuntimeError:
            return False
        snapshot = getattr(self, "_cross_beam_snapshot", None)
        if (
            snapshot is not None
            and snapshot[0]() is cache
            and len(snapshot[1]) == len(tensors)
            and all(
                saved() is tensor and version == current_version
                for (saved, version), tensor, current_version in zip(snapshot[1], tensors, versions)
            )
        ):
            return True
        # Integer views distinguish signed zeros and compare the exact cached bits.
        if not all(
            torch.equal(
                tensor[1:].view(torch.int32),
                tensor[:-1].view(torch.int32),
            )
            for tensor in tensors
        ):
            return False
        self._cross_beam_snapshot = (
            ref(cache),
            tuple((ref(tensor), version) for tensor, version in zip(tensors, versions)),
        )
        return True
