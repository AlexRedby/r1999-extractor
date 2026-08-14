import unittest

from r1999extractor.delivery import annotate_delivery


class DeliveryAnnotationTest(unittest.TestCase):
    def test_detects_urgent_fear_and_builds_model_prompts(self):
        annotation = annotate_delivery("Help! Run from the monster!", speaker="Test Hero")

        self.assertEqual(annotation["emotion"]["primary"], "fear")
        self.assertEqual(annotation["delivery"]["pace"], "fast")
        self.assertIn("chatterbox", annotation["prompt_adapters"])
        self.assertIn("Test Hero", annotation["prompt_adapters"]["generic"])

    def test_narration_defaults_to_reflective_delivery(self):
        annotation = annotate_delivery("The synthetic room is quiet.", kind="narration")

        self.assertEqual(annotation["emotion"]["primary"], "contemplation")
        self.assertEqual(annotation["delivery"]["pace"], "slow")


if __name__ == "__main__":
    unittest.main()
