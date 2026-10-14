import gzip
import hashlib
import json
import unittest
from io import BytesIO
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


def write_manifest(bundles, entries):
    path = bundles.parent / "configs" / "allmanifest.dat"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(entries, stream)


def manifest_entry(name, *filenames):
    return {"abName": name, "assets": [f"singlebg/portraits/{filename}" for filename in filenames]}


def bundle_name(name):
    return hashlib.md5(name.encode(), usedforsecurity=False).hexdigest() + ".dat"


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

    def test_manifest_selects_only_relevant_bundles(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundles = root / "bundles"
            bundles.mkdir()
            selected = bundle_name("singlebg/new_portrait_shard")
            for filename in (selected, "unrelated.dat", *PORTRAIT_BUNDLE_NAMES):
                (bundles / filename).write_bytes(b"bundle")
            write_manifest(bundles, [manifest_entry("singlebg/new_portrait_shard", "314601.png")])
            calls = []
            progress = []

            def loader(path):
                calls.append(path.name)
                return FakeEnvironment(FakeSprite("314601", "purple"), FakeSprite("314602", "red"))

            result = extract_story_portraits(
                bundles,
                ("314601.png",),
                root / "portraits",
                environment_loader=loader,
                progress=progress.append,
            )

        self.assertEqual(set(result), {"314601.png"})
        self.assertEqual(calls, [selected])
        self.assertEqual(
            [
                (value["stage"], value["processed"], value["total"], value["state"])
                for value in progress
            ],
            [
                ("portraits", 0, 1, "analyzing"),
                ("portraits", 1, 1, "analyzing"),
                ("portraits", 1, 1, "publishing"),
            ],
        )

    def test_declared_uninstalled_portrait_does_not_scan_other_bundles(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundles = root / "bundles"
            bundles.mkdir()
            (bundles / "unrelated.dat").write_bytes(b"bundle")
            write_manifest(bundles, [manifest_entry("singlebg/uninstalled", "314601.png")])
            calls = []

            def loader(path):
                calls.append(path)
                return FakeEnvironment(FakeSprite("314601", "purple"))

            result = extract_story_portraits(
                bundles,
                ("314601.png",),
                root / "portraits",
                environment_loader=loader,
            )

        self.assertEqual(result, {})
        self.assertEqual(calls, [])

    def test_manifest_keeps_preferred_and_cached_routes_with_early_progress(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundles = root / "bundles"
            bundles.mkdir()
            names = ("singlebg/headicon_room", "singlebg/new_shard")
            for name in names:
                (bundles / bundle_name(name)).write_bytes(b"bundle")
            write_manifest(bundles, [manifest_entry(name, "314601.png") for name in names])
            calls = []
            progress = []

            def loader(path):
                calls.append(path.name)
                return FakeEnvironment(FakeSprite("314601", "purple"))

            for _ in range(2):
                calls.clear()
                progress.clear()
                extract_story_portraits(
                    bundles,
                    ("314601.png",),
                    root / "portraits",
                    environment_loader=loader,
                    progress=progress.append,
                )
                self.assertEqual(calls, [PORTRAIT_BUNDLE_NAMES[0]])
                self.assertEqual(progress[-1]["processed"], 1)
                self.assertEqual(progress[-1]["total"], 2)
                self.assertEqual(progress[-1]["state"], "publishing")

    def test_malformed_manifest_and_unknown_assets_keep_fallback(self):
        manifests = (
            b"not gzip",
            gzip.compress(b"not json"),
            gzip.compress(
                json.dumps([{"abName": "singlebg/broken", "assets": "invalid"}]).encode()
            ),
            gzip.compress(json.dumps([manifest_entry("../unsafe", "314601.png")]).encode()),
            gzip.compress(json.dumps([manifest_entry("singlebg/other", "314602.png")]).encode()),
        )
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundles = root / "bundles"
            bundles.mkdir()
            (bundles / "found.dat").write_bytes(b"bundle")
            manifest = root / "configs" / "allmanifest.dat"
            manifest.parent.mkdir()
            for payload in manifests:
                with self.subTest(payload=payload[:40]):
                    manifest.write_bytes(payload)
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
                    self.assertEqual(calls, ["found.dat"])

    def test_fallback_only_searches_assets_unknown_to_the_manifest(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundles = root / "bundles"
            bundles.mkdir()
            (bundles / "found.dat").write_bytes(b"bundle")
            write_manifest(bundles, [manifest_entry("singlebg/uninstalled", "314601.png")])

            def loader(_path):
                return FakeEnvironment(FakeSprite("314601", "purple"), FakeSprite("314602", "red"))

            result = extract_story_portraits(
                bundles,
                ("314601.png", "314602.png"),
                root / "portraits",
                environment_loader=loader,
            )

        self.assertEqual(set(result), {"314602.png"})

    def test_adding_and_replacing_manifest_updates_cached_routes(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundles = root / "bundles"
            bundles.mkdir()
            names = ("singlebg/first", "singlebg/second", "singlebg/third")
            colors = dict(zip((bundle_name(name) for name in names), ("red", "green", "blue")))
            for filename in colors:
                (bundles / filename).write_bytes(b"bundle")
            calls = []

            def loader(path):
                calls.append(path.name)
                return FakeEnvironment(FakeSprite("314601", colors[path.name]))

            first = extract_story_portraits(
                bundles,
                ("314601.png",),
                root / "portraits",
                cache_key="same-story",
                environment_loader=loader,
            )
            for name in names:
                with self.subTest(name=name):
                    write_manifest(bundles, [manifest_entry(name, "314601.png")])
                    calls.clear()
                    result = extract_story_portraits(
                        bundles,
                        ("314601.png",),
                        root / "portraits",
                        cache_key="same-story",
                        environment_loader=loader,
                    )
                    self.assertEqual(calls, [bundle_name(name)])
                    self.assertEqual(set(result), set(first))
                    image = FakeSprite("314601", colors[bundle_name(name)]).image
                    output = BytesIO()
                    image.save(output, format="PNG")
                    self.assertEqual(
                        result["314601.png"], hashlib.sha256(output.getvalue()).hexdigest()
                    )

    def test_mixed_request_uses_updated_known_route_and_unknown_fallback(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundles = root / "bundles"
            bundles.mkdir()
            (bundles / "old.dat").write_bytes(b"old")
            selected = bundle_name("singlebg/new_shard")
            calls = []

            def loader(path):
                calls.append(path.name)
                if path.name == "fallback.dat":
                    return FakeEnvironment(FakeSprite("314602", "green"))
                color = "blue" if path.name == selected else "red"
                return FakeEnvironment(FakeSprite("314601", color))

            extract_story_portraits(
                bundles,
                ("314601.png",),
                root / "portraits",
                cache_key="same-story",
                environment_loader=loader,
            )
            (bundles / selected).write_bytes(b"new")
            (bundles / "fallback.dat").write_bytes(b"fallback")
            write_manifest(bundles, [manifest_entry("singlebg/new_shard", "314601.png")])
            calls.clear()
            result = extract_story_portraits(
                bundles,
                ("314601.png", "314602.png"),
                root / "portraits",
                cache_key="same-story",
                environment_loader=loader,
            )
            self.assertEqual(calls, ["old.dat", selected, "fallback.dat"])
            for filename, color in (("314601.png", "blue"), ("314602.png", "green")):
                payload = BytesIO()
                FakeSprite(Path(filename).stem, color).image.save(payload, format="PNG")
                self.assertEqual(result[filename], hashlib.sha256(payload.getvalue()).hexdigest())

    def test_windows_layout_uses_sibling_manifest_when_local_metadata_is_unusable(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "StreamingAssets"
            for local_name, sibling_name in (
                ("Windows", "PersistentRoot"),
                ("PersistentRoot", "Windows"),
            ):
                with self.subTest(local_name=local_name):
                    bundles = root / local_name / "bundles"
                    bundles.mkdir(parents=True, exist_ok=True)
                    selected = bundle_name("singlebg/sibling_shard")
                    (bundles / selected).write_bytes(b"bundle")
                    (bundles / "unrelated.dat").write_bytes(b"bundle")
                    sibling_bundles = root / sibling_name / "bundles"
                    write_manifest(
                        sibling_bundles, [manifest_entry("singlebg/sibling_shard", "314601.png")]
                    )
                    local_manifest = bundles.parent / "configs" / "allmanifest.dat"
                    local_manifest.parent.mkdir(exist_ok=True)
                    for malformed in (False, True):
                        with self.subTest(malformed=malformed):
                            local_manifest.unlink(missing_ok=True)
                            if malformed:
                                local_manifest.write_bytes(b"not gzip")
                            calls = []

                            def loader(path):
                                calls.append(path.name)
                                return FakeEnvironment(FakeSprite("314601", "purple"))

                            result = extract_story_portraits(
                                bundles,
                                ("314601.png",),
                                bundles.parent / "portraits",
                                environment_loader=loader,
                            )
                            self.assertEqual(set(result), {"314601.png"})
                            self.assertEqual(calls, [selected])

    def test_local_windows_manifest_wins_over_sibling_metadata(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "StreamingAssets"
            bundles = root / "Windows" / "bundles"
            bundles.mkdir(parents=True)
            selected = bundle_name("singlebg/local_shard")
            (bundles / selected).write_bytes(b"bundle")
            write_manifest(bundles, [manifest_entry("singlebg/local_shard", "314601.png")])
            write_manifest(
                root / "PersistentRoot" / "bundles",
                [manifest_entry("singlebg/other", "314601.png")],
            )
            calls = []

            def loader(path):
                calls.append(path.name)
                return FakeEnvironment(FakeSprite("314601", "purple"))

            result = extract_story_portraits(
                bundles,
                ("314601.png",),
                bundles.parent / "portraits",
                environment_loader=loader,
            )

        self.assertEqual(set(result), {"314601.png"})
        self.assertEqual(calls, [selected])

    def test_sibling_manifest_is_not_taken_from_an_unrelated_layout(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "unrelated"
            bundles = root / "Windows" / "bundles"
            bundles.mkdir(parents=True)
            (bundles / "found.dat").write_bytes(b"bundle")
            write_manifest(
                root / "PersistentRoot" / "bundles",
                [manifest_entry("singlebg/uninstalled", "314601.png")],
            )

            def loader(_path):
                return FakeEnvironment(FakeSprite("314601", "purple"))

            result = extract_story_portraits(
                bundles,
                ("314601.png",),
                bundles.parent / "portraits",
                environment_loader=loader,
            )

        self.assertEqual(set(result), {"314601.png"})


if __name__ == "__main__":
    unittest.main()
