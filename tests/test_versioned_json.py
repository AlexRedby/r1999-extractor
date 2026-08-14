import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.versioned_json import VersionedJSONCodec, VersionedJSONError


class VersionedJSONCodecTest(unittest.TestCase):
    def setUp(self):
        self.codec = VersionedJSONCodec("example.state", 2, "example state")

    def test_round_trip_adds_and_validates_envelope(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            document = self.codec.new(items={"one": 1})
            self.codec.write(path, document, sort_keys=True)
            loaded = self.codec.load(path)

        self.assertEqual(loaded, document)

    def test_rejects_wrong_schema_and_invalid_json(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps({"schema": "other", "schema_version": 2}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(VersionedJSONError, "Unsupported example state"):
                self.codec.load(path)

            path.write_text("not JSON", encoding="utf-8")
            with self.assertRaisesRegex(VersionedJSONError, "Unable to read example state"):
                self.codec.load(path)


if __name__ == "__main__":
    unittest.main()
