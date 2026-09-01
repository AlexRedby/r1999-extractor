import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from r1999extractor.story_portraits import (
    PORTRAIT_BUNDLE_NAMES,
    StoryPortraitError,
    extract_story_portraits,
)


class FakeType:
    name = "Sprite"


class FakeSprite:
    def __init__(self, name, color):
        self.m_Name = name
        self.image = Image.new("RGBA", (8, 8), color)


class FakeObject:
    type = FakeType()

    def __init__(self, sprite):
        self.sprite = sprite

    def read(self):
        return self.sprite


class FakeEnvironment:
    def __init__(self, *sprites):
        self.objects = tuple(FakeObject(sprite) for sprite in sprites)


class StoryPortraitTest(unittest.TestCase):
    def test_extracts_exact_sprite_and_reuses_cached_bundle(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundles = root / "bundles"
            bundles.mkdir()
            first = bundles / "a.dat"
            second = bundles / "b.dat"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            calls = []

            def loader(path):
                calls.append(path.name)
                if path.name == second.name:
                    return FakeEnvironment(FakeSprite("314601", "purple"))
                return FakeEnvironment()

            output = root / "portraits"
            initial = extract_story_portraits(
                bundles,
                ("314601.png",),
                output,
                environment_loader=loader,
            )
            calls.clear()
            cached = extract_story_portraits(
                bundles,
                ("314601",),
                output,
                environment_loader=loader,
            )

            portrait = output / "314601.png"
            digest = hashlib.sha256(portrait.read_bytes()).hexdigest()

        self.assertEqual(initial, {"314601.png": digest})
        self.assertEqual(cached, initial)
        self.assertEqual(calls, ["b.dat"])

    def test_rejects_unsafe_identity(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            root.mkdir(exist_ok=True)
            with self.assertRaisesRegex(StoryPortraitError, "Unsafe"):
                extract_story_portraits(root, ("../portrait.png",), root / "out")

    def test_prefers_the_known_headicon_bundle_without_scanning(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundles = root / "bundles"
            bundles.mkdir()
            (bundles / "a.dat").write_bytes(b"unrelated")
            preferred = bundles / PORTRAIT_BUNDLE_NAMES[0]
            preferred.write_bytes(b"portraits")
            calls = []

            def loader(path):
                calls.append(path.name)
                return FakeEnvironment(FakeSprite("314601", "purple"))

            result = extract_story_portraits(
                bundles,
                ("314601.png",),
                root / "portraits",
                environment_loader=loader,
            )

        self.assertEqual(set(result), {"314601.png"})
        self.assertEqual(calls, [PORTRAIT_BUNDLE_NAMES[0]])


if __name__ == "__main__":
    unittest.main()
