"""Export ordered Reverse: 1999 story steps for sequence-first VNTTS playback."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.live_sequence import (
    LIVE_SEQUENCE_SCHEMA,
    LIVE_SEQUENCE_SCHEMA_VERSION,
    LiveSequencePlanError,
)
from vntts_artifacts.live_sequence import (
    write_live_sequence_plan as write_shared_live_sequence_plan,
)
from vntts_artifacts.story_index import StoryIndexError, load_story_index

from r1999extractor.story_graph import StoryGraphError, ordered_step_ids, parse_story_graph
from r1999extractor.story_index import (
    Reverse1999StoryError,
    _load_unity_environment,
    clean_story_text,
    find_story_bundle,
)

_SILENT_TEXT_PATTERN = re.compile(r"[\s.…·⋯]+")


class Reverse1999LiveSequenceError(RuntimeError):
    """Raised when raw story events cannot form a safe linear sequence plan."""


def build_live_sequence_chapter(
    document, source, story_line_ids, *, group_document, language_index=2
):
    """Return one chapter plan from the game's explicit story-group edges."""
    if not isinstance(document, list) or len(document) < 3 or not isinstance(document[2], list):
        raise Reverse1999LiveSequenceError(f"Story asset {source} has an unsupported structure")
    chapter = str(source).removeprefix("json_story_step_")
    raw_events = []
    seen_sequences = set()
    bound_line_ids = set()
    for position, step in enumerate(document[2], start=1):
        if not isinstance(step, list) or len(step) < 3 or not isinstance(step[2], list):
            raise Reverse1999LiveSequenceError(
                f"Story asset {source} raw step {position} has an unsupported structure"
            )
        sequence = step[0]
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise Reverse1999LiveSequenceError(
                f"Story asset {source} raw step {position} has no integer sequence"
            )
        if sequence < 0 or sequence in seen_sequences:
            raise Reverse1999LiveSequenceError(
                f"Story asset {source} repeats or invalidates raw sequence {sequence}"
            )
        seen_sequences.add(sequence)
        payload = step[2]
        dialogue_hidden = bool(payload) and payload[0] in (0, 10)
        text = "" if dialogue_hidden else _localized(payload, 15, language_index)
        choice_targets = _choice_targets(
            step,
            source=source,
            sequence=sequence,
        )
        line_id = f"reverse1999:{chapter}:{sequence}"
        event_id = f"reverse1999:{chapter}:event:{sequence}"
        if text and _is_silent_text(text):
            kind = "silent"
            bound_line_id = None
        elif not dialogue_hidden and line_id in story_line_ids:
            kind = "speech"
            bound_line_id = line_id
            bound_line_ids.add(line_id)
        elif text:
            # The player can see this event, but the selected story index did
            # not retain a canonical English line. Pause rather than speaking a
            # producer guess or automatically skipping it.
            kind = "wait"
            bound_line_id = None
        else:
            # Raw no-text steps carry timed background, camera, audio, effect,
            # location-card or end-card actions. The game owns their timing.
            kind = "transition"
            bound_line_id = None
        if choice_targets is not None and kind == "transition":
            kind = "choice"
        raw_events.append(
            {
                "event_id": event_id,
                "sequence": sequence,
                "kind": kind,
                "line_id": bound_line_id,
                "choice_targets": choice_targets,
            }
        )
    if not raw_events:
        raise Reverse1999LiveSequenceError(f"Story asset {source} has no raw steps")
    event_sequences = dict.fromkeys(event["sequence"] for event in raw_events)
    try:
        graph = parse_story_graph(
            group_document, event_sequences, source=f"json_story_group_{chapter}"
        )
    except StoryGraphError as error:
        raise Reverse1999LiveSequenceError(str(error)) from error
    graph_sequences = set(graph).union(target for targets in graph.values() for target in targets)
    missing_graph_sequences = graph_sequences.difference(event_sequences)
    if missing_graph_sequences:
        raise Reverse1999LiveSequenceError(
            f"Story group json_story_group_{chapter} references missing raw sequence "
            f"{sorted(missing_graph_sequences)[0]}"
        )
    incoming = {target for targets in graph.values() for target in targets}
    entry_sequences = (
        list(event_sequences)
        if len(event_sequences) == 1
        else [group[0][0] for group in group_document if group and group[0][0] not in incoming]
    )
    for event in raw_events:
        targets = event["choice_targets"] or ()
        missing_targets = set(targets).difference(event_sequences).difference({0})
        if missing_targets:
            raise Reverse1999LiveSequenceError(
                f"Story asset {source} step {event['sequence']} choice targets "
                f"missing raw sequence {sorted(missing_targets)[0]}"
            )

    expected_line_ids = {
        line_id for line_id in story_line_ids if line_id.startswith(f"reverse1999:{chapter}:")
    }
    missing_line_ids = expected_line_ids.difference(bound_line_ids)
    if missing_line_ids:
        raise Reverse1999LiveSequenceError(
            f"Story asset {source} did not bind story line {sorted(missing_line_ids)[0]!r}"
        )

    raw_event_by_sequence = {event["sequence"]: event for event in raw_events}
    events = []
    ordered_graph = {sequence: graph[sequence] for sequence in entry_sequences}
    ordered_graph.update(graph)
    for sequence in ordered_step_ids(ordered_graph):
        raw_event = raw_event_by_sequence[sequence]
        choice_targets = raw_event["choice_targets"]
        if choice_targets is not None:
            successor = tuple(
                f"reverse1999:{chapter}:event:{target}" for target in choice_targets if target != 0
            )
        else:
            successor = tuple(
                raw_event_by_sequence[target]["event_id"] for target in graph[sequence]
            )
        if choice_targets is not None:
            control = "manual"
        elif raw_event["kind"] == "wait":
            control = "manual"
        elif len(successor) > 1:
            control = "manual"
            if raw_event["kind"] == "transition":
                raw_event["kind"] = "choice"
        elif not successor:
            control = "terminal"
        elif raw_event["kind"] == "transition":
            control = "passive"
        else:
            control = "automatic"
        event = {
            "event_id": raw_event["event_id"],
            "sequence": raw_event["sequence"],
            "kind": raw_event["kind"],
            "control": control,
            "successors": list(successor),
        }
        if raw_event["line_id"] is not None:
            event["line_id"] = raw_event["line_id"]
        events.append(event)
    entry_event_ids = _entry_event_ids(
        events,
        [raw_event_by_sequence[sequence]["event_id"] for sequence in entry_sequences],
        source=source,
    )
    return {
        "chapter": chapter,
        "entry_event_ids": entry_event_ids,
        "events": events,
    }


def build_live_sequence_document(
    story_documents,
    story_index_path,
    source_bundle_path,
    *,
    game_id="reverse1999",
    producer_version=None,
    language_index=2,
):
    """Build one checksum-bound plan without reading numeric gaps as events."""
    story_index_path = Path(story_index_path).expanduser().resolve()
    source_bundle_path = Path(source_bundle_path).expanduser().resolve()
    try:
        _metadata, story_lines = load_story_index(story_index_path)
        story_digest = sha256_file(story_index_path)
        source_digest = sha256_file(source_bundle_path)
    except (OSError, StoryIndexError) as error:
        raise Reverse1999LiveSequenceError(str(error)) from error
    story_line_ids = {line.line_id for line in story_lines}
    if not isinstance(story_documents, dict) or not story_documents:
        raise Reverse1999LiveSequenceError("No raw story documents were selected")
    indexed_chapters = {str(line.chapter) for line in story_lines}
    selected_documents = {
        source: document
        for source, document in story_documents.items()
        if source.startswith("json_story_step_")
        and source.removeprefix("json_story_step_") in indexed_chapters
    }
    if not selected_documents:
        raise Reverse1999LiveSequenceError(
            "No raw story document matches a chapter in the selected story index"
        )
    chapters = []
    for source, document in sorted(selected_documents.items()):
        group_source = source.replace("json_story_step_", "json_story_group_", 1)
        if group_source not in story_documents:
            raise Reverse1999LiveSequenceError(f"Story bundle does not contain {group_source!r}")
        chapters.append(
            build_live_sequence_chapter(
                document,
                source,
                story_line_ids,
                group_document=story_documents[group_source],
                language_index=language_index,
            )
        )
    return {
        "schema": LIVE_SEQUENCE_SCHEMA,
        "schema_version": LIVE_SEQUENCE_SCHEMA_VERSION,
        "game_id": game_id,
        "producer": {
            "name": "reverse1999-extractor",
            "version": producer_version or _package_version(),
        },
        "story_index_sha256": story_digest,
        "source_extract_sha256": source_digest,
        "chapters": chapters,
    }


def extract_story_documents(bundle, *, chapters=()):
    selected_chapters = {str(chapter).strip() for chapter in chapters if str(chapter).strip()}
    environment = _load_unity_environment(bundle)
    documents = {}
    for obj in environment.objects:
        if obj.type.name != "TextAsset":
            continue
        asset = obj.read()
        if not asset.m_Name.startswith(("json_story_step_", "json_story_group_")):
            continue
        chapter = asset.m_Name.rsplit("_", 1)[1]
        if selected_chapters and chapter not in selected_chapters:
            continue
        try:
            document = json.loads(asset.m_Script.lstrip("\ufeff"))
        except (TypeError, json.JSONDecodeError) as error:
            raise Reverse1999LiveSequenceError(
                f"Invalid JSON in {asset.m_Name}: {error}"
            ) from error
        if asset.m_Name in documents:
            raise Reverse1999LiveSequenceError(f"Story bundle repeats asset {asset.m_Name!r}")
        documents[asset.m_Name] = document
    chapters_to_check = selected_chapters or {source.rsplit("_", 1)[1] for source in documents}
    for chapter in sorted(chapters_to_check):
        for prefix in ("json_story_step_", "json_story_group_"):
            source = f"{prefix}{chapter}"
            if source not in documents:
                raise Reverse1999LiveSequenceError(f"Story bundle does not contain {source!r}")
    return documents


def write_live_sequence_plan(document, output, story_index_path):
    try:
        return write_shared_live_sequence_plan(
            output,
            document,
            story_index_path,
        ).path
    except LiveSequencePlanError as error:
        raise Reverse1999LiveSequenceError(str(error)) from error


def create_parser():
    parser = argparse.ArgumentParser(
        description="Export a checksum-bound VNTTS live sequence plan from raw story steps."
    )
    parser.add_argument("--story-index", type=Path, required=True)
    parser.add_argument("--resource-root", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--chapter", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    bundle = options.bundle or find_story_bundle(options.resource_root)
    if bundle is None:
        print(
            "Unable to find the installed Reverse: 1999 story bundle; pass "
            "--resource-root or --bundle",
            file=sys.stderr,
        )
        return 1
    try:
        source_before = _file_identity(bundle)
        story_before = _file_identity(options.story_index)
        documents = extract_story_documents(bundle, chapters=options.chapter)
        plan = build_live_sequence_document(
            documents,
            options.story_index,
            bundle,
        )
        if _file_identity(bundle) != source_before:
            raise Reverse1999LiveSequenceError(
                "Story bundle changed while the live sequence plan was being built"
            )
        if _file_identity(options.story_index) != story_before:
            raise Reverse1999LiveSequenceError(
                "Story index changed while the live sequence plan was being built"
            )
        output = write_live_sequence_plan(plan, options.output, options.story_index)
    except (OSError, Reverse1999StoryError, Reverse1999LiveSequenceError) as error:
        print(error, file=sys.stderr)
        return 1
    event_count = sum(len(chapter["events"]) for chapter in plan["chapters"])
    print(f"Wrote {event_count} events across {len(plan['chapters'])} chapter(s) to {output}")
    return 0


def _localized(payload, index, language_index):
    if len(payload) <= index or not isinstance(payload[index], list):
        return ""
    values = payload[index]
    if len(values) <= language_index or not isinstance(values[language_index], str):
        return ""
    return clean_story_text(values[language_index])


def _is_silent_text(text):
    return bool(text) and _SILENT_TEXT_PATTERN.fullmatch(text) is not None


def _choice_targets(step, *, source, sequence):
    raw_choices = step[10] if len(step) > 10 else []
    if raw_choices in (None, []):
        return None
    if not isinstance(raw_choices, list):
        raise Reverse1999LiveSequenceError(
            f"Story asset {source} step {sequence} has malformed choices"
        )
    targets = []
    for choice_index, choice in enumerate(raw_choices, start=1):
        if not isinstance(choice, list) or len(choice) != 11:
            raise Reverse1999LiveSequenceError(
                f"Story asset {source} step {sequence} choice {choice_index} "
                "has an unsupported structure"
            )
        target = choice[10]
        if not isinstance(target, int) or isinstance(target, bool) or target < 0:
            raise Reverse1999LiveSequenceError(
                f"Story asset {source} step {sequence} choice {choice_index} has an invalid target"
            )
        if target not in targets:
            targets.append(target)
    return tuple(targets)


def _entry_event_ids(events, entries, *, source):
    by_id = {event["event_id"]: event for event in events}
    if not entries:
        raise Reverse1999LiveSequenceError(f"Story asset {source} has no game entry step")
    reachable = set()

    def visit(start):
        pending = [start]
        while pending:
            event_id = pending.pop()
            if event_id in reachable:
                continue
            reachable.add(event_id)
            pending.extend(by_id[event_id]["successors"])

    for event_id in entries:
        visit(event_id)
    unreachable = set(by_id).difference(reachable)
    if unreachable:
        raise Reverse1999LiveSequenceError(
            f"Story asset {source} has raw event {sorted(unreachable)[0]!r} "
            "unreachable from game entries"
        )
    return entries


def _package_version():
    try:
        return version("reverse1999-extractor")
    except PackageNotFoundError:
        return "0+unknown"


def _file_identity(path):
    path = Path(path).expanduser().resolve()
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
