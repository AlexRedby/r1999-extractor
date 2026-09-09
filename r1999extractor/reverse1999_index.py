import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json

from r1999extractor.reverse1999_config import packaged_macos_resource_roots
from r1999extractor.reverse1999_voice_import import (
    find_game_audio_directory,
    is_scene_audio_bank,
)
from r1999extractor.settings import get_local_data_directory
from r1999extractor.wwise import WwiseBankError, inspect_bank

index_version = 6
default_output = get_local_data_directory() / "reverse1999" / "english-bank-index.json"
npc_id_pattern = re.compile(r"npc[_-]?(\d{4,})", re.IGNORECASE)
chapter_pattern = re.compile(r"chapter[_-]?(\d+)", re.IGNORECASE)


class Reverse1999IndexError(RuntimeError):
    pass


def audio_bank_layout(root):
    """Recognize installed English base/download overlays, not other languages."""
    root = Path(root).expanduser().resolve()
    if (
        root.name == "en"
        and root.parent.name == "Windows"
        and root.parent.parent.name == "audios"
        and root.parents[2].name in {"PersistentRoot", "Windows"}
        and root.parents[3].name == "StreamingAssets"
    ):
        root = root.parents[3]
    if root.name == "StreamingAssets":
        directories = tuple(
            root / folder / "audios" / "Windows" / "en"
            for folder in ("PersistentRoot", "Windows")
            if (root / folder / "audios" / "Windows" / "en").is_dir()
        )
        if not directories:
            raise Reverse1999IndexError(f"No English audio directories in {root}")
        return root, directories
    if tuple(reversed(root.parts[-6:])) == ("en", "iOS", "audios", "iOS", "ResLib", "Documents"):
        packaged = tuple(
            candidate / "audios/iOS/en"
            for candidate in packaged_macos_resource_roots()
            if (candidate / "audios/iOS/en").is_dir()
        )
        return root, (root, *packaged)
    return root, (root,)


def discover_bank_files(root):
    root, directories = audio_bank_layout(root)
    selected = {}
    for directory in directories:
        current = {}
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.casefold() != ".bnk":
                continue
            path.resolve().relative_to(directory.resolve())
            key = path.name.casefold()
            if key in current:
                raise Reverse1999IndexError(f"Duplicate bank filename in {directory}: {path.name}")
            current[key] = path
        # Downloaded banks override the packaged copy by filename.
        for key, path in current.items():
            selected.setdefault(key, path)
    return tuple(sorted(selected.values(), key=lambda path: path.as_posix()))


def bank_source_root(index, entry):
    root = Path(index["game_audio_directory"]).expanduser().resolve()
    source = entry.get("source_directory")
    if source is None:
        return root
    if not isinstance(source, str) or not Path(source).is_absolute():
        raise Reverse1999IndexError("Unsafe bank source directory")
    source = Path(source).resolve()
    if source not in audio_bank_layout(root)[1]:
        raise Reverse1999IndexError("Bank source directory is not an installed audio root")
    return source


def bank_source_path(index, entry):
    root = bank_source_root(index, entry)
    relative = entry.get("path")
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or "\\" in relative
    ):
        raise Reverse1999IndexError("Unsafe bank source path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise Reverse1999IndexError("Bank source path escapes audio root")
    return path


def bank_external_media_root(index, entry):
    root = bank_source_root(index, entry)
    relative = entry.get("media_directory")
    if relative is None:
        return root.parent / "Media"
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or "\\" in relative
    ):
        raise Reverse1999IndexError("Unsafe bank external-media directory")
    directory = (root / relative).resolve()
    try:
        directory.relative_to(root)
    except ValueError as error:
        raise Reverse1999IndexError("Bank external-media directory escapes audio root") from error
    return directory


def bank_index_staleness_reasons(index, game_audio_directory=None):
    if not isinstance(index, dict):
        return ["bank index is not a JSON object"]
    if index.get("version") != index_version:
        return [f"bank index version {index.get('version')!r} does not match {index_version}"]
    configured_root = index.get("game_audio_directory")
    if not isinstance(configured_root, str) or not configured_root:
        return ["bank index has no game audio directory"]
    try:
        root, directories = audio_bank_layout(game_audio_directory or configured_root)
    except (OSError, ValueError, Reverse1999IndexError) as error:
        return [f"unable to locate installed audio: {error}"]
    if str(root) != str(Path(configured_root).expanduser().resolve()):
        return [f"game audio directory changed: {configured_root} -> {root}"]
    if not root.is_dir():
        return [f"game audio directory does not exist: {root}"]

    entries = index.get("banks")
    if not isinstance(entries, list):
        return ["bank index has no valid bank list"]
    if isinstance(index.get("bank_count"), int) and index["bank_count"] != len(entries):
        return ["bank index count does not match its bank list"]
    stored = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return ["bank index contains an invalid bank entry"]
        relative_path = entry.get("path")
        size = entry.get("size")
        mtime_ns = entry.get("mtime_ns")
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(mtime_ns, int)
            or isinstance(mtime_ns, bool)
        ):
            return ["bank index entry is missing its source fingerprint"]
        if relative_path in stored:
            return [f"bank index contains duplicate path: {relative_path}"]
        stored[relative_path] = (size, mtime_ns, entry.get("source_directory"))

    current = {}
    try:
        for path in discover_bank_files(root):
            stat = path.stat()
            source = (
                root
                if path.is_relative_to(root)
                else next(directory for directory in directories if path.is_relative_to(directory))
            )
            current[path.relative_to(source).as_posix()] = (
                stat.st_size,
                stat.st_mtime_ns,
                str(source) if source != root else None,
            )
    except (OSError, ValueError, Reverse1999IndexError) as error:
        return [f"unable to fingerprint installed banks: {error}"]

    added = sorted(set(current) - set(stored), key=str.casefold)
    removed = sorted(set(stored) - set(current), key=str.casefold)
    changed = sorted(
        (path for path in set(current) & set(stored) if current[path] != stored[path]),
        key=str.casefold,
    )
    reasons = []
    for label, paths in (
        ("new", added),
        ("removed", removed),
        ("changed", changed),
    ):
        if not paths:
            continue
        examples = ", ".join(paths[:3])
        if len(paths) > 3:
            examples += f", and {len(paths) - 3} more"
        reasons.append(f"{len(paths)} {label} bank(s): {examples}")
    return reasons


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Index installed Reverse: 1999 English Wwise banks for NPC voice "
            "discovery. Existing unchanged entries are reused."
        )
    )
    parser.add_argument(
        "--game-audio-directory",
        type=Path,
        help="Directory containing the installed game's English .bnk files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
        help="JSON index path.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reinspect every bank instead of reusing unchanged entries.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report whether the existing index matches the installed banks.",
    )
    return parser


def classify_bank(filename):
    stem = Path(filename).stem.casefold()
    tags = []
    if "npc" in stem:
        tags.append("npc")
    if "plotvoc" in stem or "story" in stem or chapter_pattern.search(stem):
        tags.append("story")
    if "activity" in stem:
        tags.append("activity")
    if "voc" in stem or "voice" in stem:
        tags.append("voice")
    if is_scene_audio_bank(filename):
        tags.append("scene-audio")

    tag_set = set(tags)
    if "scene-audio" in tag_set:
        category = "scene-audio-npc"
    elif {"npc", "activity"} <= tag_set:
        category = "activity-npc"
    elif {"npc", "story"} <= tag_set:
        category = "story-npc"
    elif "npc" in tag_set:
        category = "npc"
    elif "voice" in tag_set:
        category = "voice"
    else:
        category = "other"
    return category, tags


def inspect_bank_entry(bank, root, *, inspector=inspect_bank):
    stat = bank.stat()
    relative_path = bank.relative_to(root).as_posix()
    category, tags = classify_bank(bank.name)
    entry = {
        "path": relative_path,
        "filename": bank.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "category": category,
        "tags": tags,
        "npc_ids": sorted(set(npc_id_pattern.findall(bank.stem))),
        "chapters": sorted({int(value) for value in chapter_pattern.findall(bank.stem)}),
    }
    try:
        summary = inspector(bank)
    except (OSError, WwiseBankError) as error:
        entry["error"] = str(error)
        return entry

    entry.update(
        {
            "bank_version": summary.bank_version,
            "sections": list(summary.sections),
            "media_count": summary.media_count,
            "embedded_media_ids": list(summary.media_ids),
            "embedded_media_bytes": summary.embedded_media_bytes,
            "hirc_object_count": summary.hirc_object_count,
            "event_count": summary.event_count,
            "events": [
                {
                    "event_id": route.event_id,
                    "action_ids": list(route.action_ids),
                    "actions": [
                        {
                            "action_id": action.action_id,
                            "action_type": action.action_type,
                            "target_id": action.target_id,
                        }
                        for action in route.actions
                    ],
                    "sound_ids": list(route.sound_ids),
                    "media_ids": list(route.media_ids),
                    "streamed_media_ids": list(route.streamed_media_ids),
                }
                for route in summary.event_routes
            ],
        }
    )
    return entry


def load_reusable_entries(path, root):
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if previous.get("version") != index_version or previous.get("game_audio_directory") != str(
        root
    ):
        return {}
    return {
        entry["path"]: entry
        for entry in previous.get("banks", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }


def build_bank_index(
    game_audio_directory,
    *,
    output=None,
    force=False,
    inspector=inspect_bank,
    progress=None,
):
    root = Path(game_audio_directory).expanduser().resolve()
    if not root.is_dir():
        raise Reverse1999IndexError(f"Game audio directory does not exist: {root}")
    root, directories = audio_bank_layout(root)
    banks = discover_bank_files(root)
    if not banks:
        raise Reverse1999IndexError(f"No .bnk files found in {root}")

    output = Path(output or default_output).expanduser().resolve()
    reusable = {} if force else load_reusable_entries(output, root)
    entries = []
    reused_count = 0
    progress = progress or (lambda _current, _total, _bank, _reused: None)
    for current, bank in enumerate(banks, start=1):
        source = (
            root
            if bank.is_relative_to(root)
            else next(directory for directory in directories if bank.is_relative_to(directory))
        )
        relative_path = bank.relative_to(source).as_posix()
        stat = bank.stat()
        previous = reusable.get(relative_path)
        reused = bool(
            previous
            and previous.get("size") == stat.st_size
            and previous.get("mtime_ns") == stat.st_mtime_ns
            and previous.get("source_directory") == (str(source) if source != root else None)
        )
        if reused:
            entry = previous
            reused_count += 1
        else:
            entry = inspect_bank_entry(bank, source, inspector=inspector)
        if source != root:
            entry["source_directory"] = str(source)
        if root.name == "StreamingAssets":
            source = next(directory for directory in directories if bank.is_relative_to(directory))
            entry["media_directory"] = (source.parent / "Media").relative_to(root).as_posix()
        entries.append(entry)
        progress(current, len(banks), bank, reused)

    categories = Counter(entry["category"] for entry in entries)
    npc_banks = defaultdict(list)
    for entry in entries:
        for npc_id in entry["npc_ids"]:
            npc_banks[npc_id].append(entry["path"])
    index = {
        "version": index_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "game_audio_directory": str(root),
        "bank_count": len(entries),
        "reused_count": reused_count,
        "error_count": sum("error" in entry for entry in entries),
        "categories": dict(sorted(categories.items())),
        "npc_banks": dict(sorted(npc_banks.items())),
        "banks": entries,
    }
    atomic_write_json(output, index)
    return index, output


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    game_audio_directory = arguments.game_audio_directory or find_game_audio_directory()
    if game_audio_directory is None:
        print(
            "Unable to find Reverse: 1999 game audio; pass --game-audio-directory",
            file=sys.stderr,
        )
        return 1

    def progress(current, total, bank, reused):
        if current == total or current % 100 == 0:
            action = "Reused" if reused else "Indexed"
            print(f"{action} {current}/{total}: {bank.name}")

    if arguments.check:
        try:
            index = json.loads(arguments.output.expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"Bank index is stale: unable to read {arguments.output}: {error}")
            return 1
        reasons = bank_index_staleness_reasons(index, game_audio_directory)
        if reasons:
            print("Bank index is stale: " + "; ".join(reasons))
            return 1
        print(f"Bank index is up to date: {arguments.output.expanduser().resolve()}")
        return 0

    try:
        index, output = build_bank_index(
            game_audio_directory,
            output=arguments.output,
            force=arguments.force,
            progress=progress,
        )
    except Reverse1999IndexError as error:
        print(error, file=sys.stderr)
        return 1

    print(
        f"Indexed {index['bank_count']} banks ({index['reused_count']} reused, "
        f"{index['error_count']} errors) into {output}"
    )
    return 0
