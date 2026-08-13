import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from r1999extractor.atomic_io import atomic_output_path
from r1999extractor.settings import get_local_data_directory

story_asset_name = "configs/story"
story_bundle_filename = f"{hashlib.md5(story_asset_name.encode()).hexdigest()}.dat"
story_index_version = 1
default_output = get_local_data_directory() / "reverse1999" / "story-index.jsonl"


class Reverse1999StoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoryLine:
    record_type: str
    line_id: str
    chapter: str
    sequence: int
    speaker: str
    text: str
    source: str
    portrait: str | None
    source_voice_id: str | None
    display_seconds: float | None
    kind: str


def _localized(value, language_index):
    if not isinstance(value, list) or len(value) <= language_index:
        return ""
    localized = value[language_index]
    return localized.strip() if isinstance(localized, str) else ""


def parse_story_document(document, source, *, language_index=2):
    if not isinstance(document, list) or len(document) < 3 or not isinstance(document[2], list):
        raise Reverse1999StoryError(f"Story asset {source} has an unsupported structure")
    chapter = source.removeprefix("json_story_step_")
    lines = []
    for position, step in enumerate(document[2]):
        if not isinstance(step, list) or len(step) < 3 or not isinstance(step[2], list):
            continue
        payload = step[2]
        if len(payload) <= 15:
            continue
        text = _localized(payload[15], language_index)
        if not text:
            continue
        speaker = _localized(payload[11], language_index) or "Narrator"
        try:
            sequence = int(step[0])
        except (TypeError, ValueError):
            sequence = position
        portrait = payload[13].strip() if isinstance(payload[13], str) else ""
        source_voice_id = payload[14].strip() if isinstance(payload[14], str) else ""
        display_seconds = None
        if isinstance(payload[1], list) and len(payload[1]) > language_index:
            value = payload[1][language_index]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                display_seconds = float(value)
        lines.append(
            StoryLine(
                record_type="line",
                line_id=f"reverse1999:{chapter}:{sequence}",
                chapter=chapter,
                sequence=sequence,
                speaker=speaker,
                text=text,
                source=source,
                portrait=portrait or None,
                source_voice_id=source_voice_id or None,
                display_seconds=display_seconds,
                kind="narration" if speaker == "Narrator" else "dialogue",
            )
        )
    return lines


def find_game_resource_root(home=None, environment=None):
    home = Path.home() if home is None else Path(home)
    environment = os.environ if environment is None else environment
    candidates = []
    containers = home / "Library" / "Containers"
    candidates.extend(containers.glob("*/Data/Documents/ResLib/iOS"))
    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        candidates.extend(Path(local_app_data).glob("**/ResLib/*"))
    for candidate in candidates:
        if (candidate / "bundles" / story_bundle_filename).is_file():
            return candidate.resolve()
    return None


def find_story_bundle(resource_root=None):
    root = (
        find_game_resource_root()
        if resource_root is None
        else Path(resource_root).expanduser().resolve()
    )
    if root is None:
        return None
    candidate = root / "bundles" / story_bundle_filename
    return candidate if candidate.is_file() else None


def _load_unity_environment(path):
    try:
        import UnityPy
    except ImportError as error:
        raise Reverse1999StoryError(
            "UnityPy is required for story extraction; install the project dependencies"
        ) from error
    data = Path(path).read_bytes()
    header = data.find(b"UnityFS")
    if header < 0:
        raise Reverse1999StoryError(f"UnityFS header not found in {path}")
    try:
        return UnityPy.load(data[header:])
    except Exception as error:
        raise Reverse1999StoryError(f"Unable to load story bundle {path}: {error}") from error


def extract_story_lines(bundle, *, language_index=2, progress=None):
    progress = progress or (lambda _current, _total, _source: None)
    environment = _load_unity_environment(bundle)
    assets = [obj for obj in environment.objects if obj.type.name == "TextAsset"]
    lines = []
    story_assets = []
    for obj in assets:
        asset = obj.read()
        if asset.m_Name.startswith("json_story_step_"):
            story_assets.append(asset)
    story_assets.sort(key=lambda asset: asset.m_Name)
    for current, asset in enumerate(story_assets, start=1):
        try:
            document = json.loads(asset.m_Script.lstrip("\ufeff"))
        except (TypeError, json.JSONDecodeError) as error:
            raise Reverse1999StoryError(f"Invalid JSON in {asset.m_Name}: {error}") from error
        lines.extend(parse_story_document(document, asset.m_Name, language_index=language_index))
        progress(current, len(story_assets), asset.m_Name)
    lines.sort(key=lambda line: (line.chapter, line.sequence, line.line_id))
    return lines


def write_story_index(lines, output=default_output, *, bundle=None):
    output = Path(output).expanduser().resolve()
    metadata = {
        "record_type": "metadata",
        "schema": "vntts.story-index",
        "schema_version": story_index_version,
        "game": "Reverse: 1999",
        "language": "en",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_bundle": str(Path(bundle).resolve()) if bundle else None,
        "line_count": len(lines),
    }
    with atomic_output_path(output) as temporary:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")
            for line in lines:
                stream.write(json.dumps(asdict(line), ensure_ascii=False, sort_keys=True) + "\n")
    return output


def create_parser():
    parser = argparse.ArgumentParser(
        description="Extract local Reverse: 1999 story text into a generic VNTTS JSONL index."
    )
    parser.add_argument(
        "--resource-root", type=Path, help="Installed ResLib platform directory containing bundles/"
    )
    parser.add_argument(
        "--bundle", type=Path, help="Story Unity bundle; overrides automatic discovery"
    )
    parser.add_argument("--output", type=Path, default=default_output)
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    bundle = options.bundle or find_story_bundle(options.resource_root)
    if bundle is None:
        print(
            "Unable to find the installed Reverse: 1999 story bundle; pass --resource-root or --bundle",
            file=sys.stderr,
        )
        return 1
    try:
        lines = extract_story_lines(
            bundle,
            progress=lambda current, total, source: (
                print(f"Parsed {current}/{total}: {source}")
                if current == total or current % 250 == 0
                else None
            ),
        )
        output = write_story_index(lines, options.output, bundle=bundle)
    except (OSError, Reverse1999StoryError) as error:
        print(error, file=sys.stderr)
        return 1
    dialogue = sum(line.kind == "dialogue" for line in lines)
    print(
        f"Wrote {len(lines)} lines ({dialogue} dialogue, {len(lines) - dialogue} narration) to {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
