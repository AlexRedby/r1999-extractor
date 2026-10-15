import copy
import unittest
from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import patch

if find_spec("torch") is None or find_spec("transformers") is None:
    torch = None
else:
    import torch
    from transformers import WhisperForConditionalGeneration
    from transformers.generation.logits_process import (
        SuppressTokensAtBeginLogitsProcessor,
        SuppressTokensLogitsProcessor,
        WhisperTimeStampLogitsProcessor,
    )
    from transformers.models.whisper.generation_whisper import _get_attr_from_logit_processors

    from r1999extractor.whisper_beam_cache import (
        CpuWhisperForConditionalGeneration,
        _CpuSuppressTokensLogitsProcessor,
        isin_mps_friendly,
    )


@unittest.skipIf(torch is None, "Optional Torch and Transformers dependencies are absent")
class WhisperLogitsCacheTest(unittest.TestCase):
    def _processor(self, tokens=(1, 3)):
        return _CpuSuppressTokensLogitsProcessor(SuppressTokensLogitsProcessor(tokens))

    def _assert_native_bits(self, processor, scores):
        inputs = torch.zeros((scores.shape[0], 1), dtype=torch.int64)
        expected = SuppressTokensLogitsProcessor.__call__(processor, inputs, scores)
        observed = processor(inputs, scores)
        self.assertTrue(
            torch.equal(
                expected.contiguous().view(torch.uint8), observed.contiguous().view(torch.uint8)
            )
        )

    def test_preserves_bits_for_special_scores_and_token_lists(self):
        values = [0.0, -0.0, float("nan"), float("inf"), -float("inf"), 1.0]
        for dtype in (torch.float16, torch.bfloat16, torch.float32, torch.float64):
            for tokens in ((), (1, 3), (-1, 1, 1, 99)):
                with self.subTest(dtype=dtype, tokens=tokens):
                    scores = torch.tensor([values, values[::-1]], dtype=dtype)
                    self._assert_native_bits(self._processor(tokens), scores)
                    self._assert_native_bits(self._processor(tokens), scores[:, ::2])

    def test_reuses_mask_until_tokens_or_vocab_change(self):
        processor = self._processor()
        inputs = torch.zeros((2, 1), dtype=torch.int64)
        with patch(
            "r1999extractor.whisper_beam_cache.isin_mps_friendly", wraps=isin_mps_friendly
        ) as membership:
            processor(inputs, torch.zeros((2, 6)))
            processor(inputs, torch.ones((2, 6)))
            self.assertEqual(membership.call_count, 1)
            processor.suppress_tokens[0] = 2
            self._assert_native_bits(processor, torch.zeros((2, 6)))
            self.assertEqual(membership.call_count, 2)
            processor.suppress_tokens = processor.suppress_tokens.clone()
            processor(inputs, torch.zeros((2, 6)))
            self.assertEqual(membership.call_count, 2)
            processor.suppress_tokens = torch.tensor([4, 5])
            self._assert_native_bits(processor, torch.zeros((2, 6)))
            self.assertEqual(membership.call_count, 3)
            processor(inputs, torch.zeros((2, 7)))
            self.assertEqual(membership.call_count, 4)

    def test_rechecks_mutations_that_do_not_update_tensor_versions(self):
        for case in ("data", "numpy"):
            with self.subTest(case=case):
                processor = self._processor()
                self._assert_native_bits(processor, torch.zeros((2, 6)))
                if case == "data":
                    processor.suppress_tokens.data[0] = 2
                else:
                    processor.suppress_tokens.numpy()[0] = 2
                self._assert_native_bits(processor, torch.zeros((2, 6)))

    def test_unversioned_inference_tokens_keep_exact_mask_checks(self):
        with torch.inference_mode():
            processor = self._processor()
            self._assert_native_bits(processor, torch.zeros((2, 6)))
            processor.suppress_tokens[0] = 2
            self._assert_native_bits(processor, torch.zeros((2, 6)))

    def test_unsupported_tensors_use_native(self):
        for case in ("device", "dtype", "shape"):
            with self.subTest(case=case):
                processor = self._processor()
                scores = torch.zeros((2, 6))
                if case == "device":
                    scores = torch.empty((2, 6), device="meta")
                elif case == "dtype":
                    processor.suppress_tokens = processor.suppress_tokens.to(torch.int32)
                elif case == "shape":
                    scores = scores[0]
                with patch.object(
                    SuppressTokensLogitsProcessor, "__call__", return_value=scores
                ) as native:
                    self.assertIs(processor(torch.zeros((2, 1)), scores), scores)
                    native.assert_called_once()

    def _model(self, model_type):
        model = model_type.__new__(model_type)
        torch.nn.Module.__init__(model)
        return model

    def _config(self):
        return SimpleNamespace(
            return_timestamps=True,
            suppress_tokens=[1, 3],
            begin_suppress_tokens=[2],
            no_speech_threshold=None,
            no_timestamps_token_id=10,
            eos_token_id=9,
            max_initial_timestamp_index=2,
        )

    def test_native_processor_lifecycle_and_caller_ownership(self):
        model = self._model(CpuWhisperForConditionalGeneration)
        native = self._model(WhisperForConditionalGeneration)
        caller = SuppressTokensLogitsProcessor([4])
        supplied = [caller]
        config = self._config()
        expected_config = copy.deepcopy(config)
        expected = native._retrieve_logit_processors(expected_config, supplied, 1, 5, "cpu")
        observed = model._retrieve_logit_processors(config, supplied, 1, 5, "cpu")
        self.assertEqual(config.__dict__, expected_config.__dict__)
        self.assertEqual(supplied, [caller])
        self.assertIs(observed[-1], caller)
        self.assertIs(type(caller), SuppressTokensLogitsProcessor)
        self.assertIs(type(observed[0]), SuppressTokensAtBeginLogitsProcessor)
        self.assertIs(type(observed[1]), _CpuSuppressTokensLogitsProcessor)
        self.assertIs(type(observed[2]), WhisperTimeStampLogitsProcessor)
        self.assertIs(
            _get_attr_from_logit_processors(
                observed, SuppressTokensLogitsProcessor, "suppress_tokens"
            ),
            observed[1].suppress_tokens,
        )
        for begin_index, inputs in ((1, [[0], [0]]), (2, [[0, 11], [0, 1]])):
            for processors in (expected, observed):
                for processor in processors:
                    if hasattr(processor, "set_begin_index"):
                        processor.set_begin_index(begin_index)
            results = []
            for processors in (expected, observed):
                scores = torch.arange(30, dtype=torch.float32).reshape(2, 15)
                for processor in processors:
                    scores = processor(torch.tensor(inputs), scores)
                results.append(scores)
            self.assertTrue(torch.equal(results[0].view(torch.int32), results[1].view(torch.int32)))
        self.assertIs(model._retrieve_logit_processors(config, supplied, 2, 5, "cpu")[-1], caller)

    def test_empty_and_non_cpu_native_paths_stay_native(self):
        model = self._model(CpuWhisperForConditionalGeneration)
        config = self._config()
        processors = model._retrieve_logit_processors(config, None, 1, 5, "meta")
        self.assertIs(type(processors[1]), SuppressTokensLogitsProcessor)
        config.return_timestamps = False
        self.assertIsNone(model._retrieve_logit_processors(config, None, 1, 5, "cpu"))


if __name__ == "__main__":
    unittest.main()
