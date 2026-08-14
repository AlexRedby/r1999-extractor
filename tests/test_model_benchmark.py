import unittest

from r1999extractor.model_benchmark import select_representative_items


class ModelBenchmarkTest(unittest.TestCase):
    def test_selects_across_emotions_before_repeating_a_bucket(self):
        items = [
            {
                "queue_id": f"neutral-{index}",
                "action": "generate",
                "emotion": {"primary": "neutral"},
            }
            for index in range(4)
        ]
        items.extend(
            [
                {
                    "queue_id": "fear-1",
                    "action": "generate",
                    "emotion": {"primary": "fear"},
                },
                {
                    "queue_id": "ignored",
                    "action": "manual_review",
                    "emotion": {"primary": "anger"},
                },
            ]
        )

        selected = select_representative_items(items, 3)

        self.assertEqual([item["queue_id"] for item in selected[:2]], ["fear-1", "neutral-0"])
        self.assertEqual(selected[2]["queue_id"], "neutral-1")


if __name__ == "__main__":
    unittest.main()
