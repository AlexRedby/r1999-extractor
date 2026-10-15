import unittest
from importlib.util import find_spec
from unittest.mock import patch

if find_spec("torch") is None or find_spec("transformers") is None:
    torch = None
else:
    import torch
    from transformers.cache_utils import DynamicCache, EncoderDecoderCache

    from r1999extractor.whisper_beam_cache import CpuWhisperForConditionalGeneration


@unittest.skipIf(torch is None, "Optional Torch and Transformers dependencies are absent")
class WhisperBeamCacheTest(unittest.TestCase):
    def setUp(self):
        self.model = CpuWhisperForConditionalGeneration.__new__(CpuWhisperForConditionalGeneration)
        torch.nn.Module.__init__(self.model)

    def _cache(self, rows=5, *, dtype=None, device="cpu"):
        cache = EncoderDecoderCache(DynamicCache(), DynamicCache())
        for index in range(2):
            own = torch.arange(rows * 6, dtype=dtype or torch.float32, device=device).reshape(
                rows, 1, 2, 3
            )
            cross = torch.arange(12, dtype=dtype or torch.float32, device=device).reshape(
                1, 1, 4, 3
            )
            cache.self_attention_cache.update(own, own + 10, index)
            cache.cross_attention_cache.update(
                cross.repeat(rows, 1, 1, 1), (cross + 10).repeat(rows, 1, 1, 1), index
            )
        return cache

    def _reorder(self, cache, *, indices=None, reused):
        if indices is None:
            indices = torch.tensor([4, 0, 0, 2, 1], dtype=torch.int32)
        own = [layer.keys.clone() for layer in cache.self_attention_cache.layers]
        cross = [
            (layer.keys, layer.keys.clone(), layer.values, layer.values.clone())
            for layer in cache.cross_attention_cache.layers
        ]
        with patch.object(
            cache.cross_attention_cache,
            "reorder_cache",
            wraps=cache.cross_attention_cache.reorder_cache,
        ) as native:
            self.assertIs(self.model._reorder_cache(cache, indices), cache)
        self.assertEqual(native.call_count, 0 if reused else 1)
        for layer, before in zip(cache.self_attention_cache.layers, own):
            self.assertTrue(torch.equal(layer.keys, before.index_select(0, indices)))
        for layer, (key, before_key, value, before_value) in zip(
            cache.cross_attention_cache.layers, cross
        ):
            self.assertTrue(torch.equal(layer.keys, before_key.index_select(0, indices)))
            self.assertTrue(torch.equal(layer.values, before_value.index_select(0, indices)))
            if reused:
                self.assertIs(layer.keys, key)
                self.assertIs(layer.values, value)

    def test_reuses_identical_bits_for_int32_and_int64_beams(self):
        for dtype in (torch.int32, torch.int64):
            with self.subTest(dtype=dtype):
                self._reorder(
                    self._cache(),
                    indices=torch.tensor([4, 0, 0, 2, 1], dtype=dtype),
                    reused=True,
                )

    def test_rechecks_only_after_cache_or_tensor_changes(self):
        cache = self._cache()
        self._reorder(cache, reused=True)
        with patch("r1999extractor.whisper_beam_cache.torch.equal", wraps=torch.equal) as equal:
            self.assertTrue(self.model._can_reuse_cross_cache(cache, torch.arange(5)))
            equal.assert_not_called()
            layer = cache.cross_attention_cache.layers[1]
            layer.values = layer.values.clone()
            self.assertTrue(self.model._can_reuse_cross_cache(cache, torch.arange(5)))
            self.assertEqual(equal.call_count, 4)
            equal.reset_mock()
            cache.reset()
            self.assertTrue(self.model._can_reuse_cross_cache(cache, torch.arange(5)))
            self.assertEqual(equal.call_count, 4)
            equal.reset_mock()
            self.assertTrue(self.model._can_reuse_cross_cache(self._cache(), torch.arange(5)))
            self.assertEqual(equal.call_count, 4)

    def test_rejects_key_value_and_signed_zero_differences(self):
        for field, value in (("keys", 1.0), ("values", -0.0)):
            with self.subTest(field=field):
                cache = self._cache()
                tensor = getattr(cache.cross_attention_cache.layers[1], field)
                tensor.zero_()
                tensor[1, 0, 0, 0] = value
                self._reorder(cache, reused=False)

    def test_rechecks_replacement_and_in_place_mutation(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                cache = self._cache()
                self._reorder(cache, reused=True)
                layer = cache.cross_attention_cache.layers[0]
                if replace:
                    layer.keys = layer.keys.clone()
                layer.keys[2, 0, 0, 0] += 1
                self._reorder(cache, reused=False)

    def test_distinct_audio_batch_falls_back(self):
        cache = self._cache(rows=10)
        for layer in cache.cross_attention_cache.layers:
            layer.keys[5:] += 1
        self._reorder(cache, indices=torch.arange(9, -1, -1), reused=False)

    def test_identical_rows_are_safe_even_across_multiple_inputs(self):
        self._reorder(self._cache(rows=10), indices=torch.arange(9, -1, -1), reused=True)

    def test_unsupported_dtype_shape_stride_and_cache_fall_back(self):
        for case in ("dtype", "shape", "stride", "cache"):
            with self.subTest(case=case):
                cache = self._cache(dtype=torch.float64 if case == "dtype" else None)
                layer = cache.cross_attention_cache.layers[0]
                if case == "shape":
                    layer.keys = layer.keys.squeeze(1)
                elif case == "stride":
                    layer.values = layer.values.transpose(-1, -2)
                elif case == "cache":

                    class OtherCache(DynamicCache):
                        pass

                    other = OtherCache()
                    other.layers = cache.cross_attention_cache.layers
                    cache.cross_attention_cache = other
                self._reorder(cache, reused=False)

    def test_non_cpu_and_unversioned_tensors_fall_back(self):
        for case in ("device", "inference"):
            with self.subTest(case=case):
                if case == "device":
                    cache = self._cache(device="meta")
                else:
                    with torch.inference_mode():
                        cache = self._cache()
                self.assertFalse(self.model._can_reuse_cross_cache(cache, torch.arange(5)))
                with patch.object(cache, "reorder_cache", wraps=cache.reorder_cache) as native:
                    self.model._reorder_cache(cache, torch.arange(5))
                    native.assert_called_once()

    def test_invalid_indices_and_row_count_use_native_validation(self):
        cases = (
            torch.tensor([-1, 0, 1, 2, 3]),
            torch.tensor([5, 0, 1, 2, 3]),
            torch.arange(5).reshape(1, 5),
            torch.arange(5, dtype=torch.float32),
            torch.arange(4),
            torch.tensor([0]),
        )
        for indices in cases:
            with self.subTest(indices=indices):
                cache = self._cache()
                self.assertFalse(self.model._can_reuse_cross_cache(cache, indices))
                with patch.object(cache, "reorder_cache", side_effect=ValueError("native")):
                    with self.assertRaisesRegex(ValueError, "native"):
                        self.model._reorder_cache(cache, indices)

    def test_original_index_error_propagates(self):
        with self.assertRaises((IndexError, RuntimeError)):
            self.model._reorder_cache(self._cache(), torch.tensor([-1, 0, 1, 2, 3]))


if __name__ == "__main__":
    unittest.main()
