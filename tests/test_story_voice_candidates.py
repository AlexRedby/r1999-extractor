import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, get_ident
from unittest.mock import patch

import numpy as np

from r1999extractor.reverse1999_index import index_version
from r1999extractor.reverse1999_voice_import import ImportedReference
from r1999extractor.story_audio import wwise_event_id
from r1999extractor.story_voice_candidates import (
    BankSnapshot,
    StoryVoiceCandidateError,
    _collect_story_voice_inventory,
    _prepare_references_concurrently,
    affected_story_line_counts,
    build_story_voice_candidates,
    collect_story_voice_lines,
    is_manual_playable_too_long_candidate,
)
from r1999extractor.voice_reference_quality import (
    UnusableVoiceReferenceError,
    VoiceReferenceMetrics,
    VoiceReferenceQualityError,
)
from r1999extractor.wwise import AudioConversionError
from tests.test_voice_reference_quality import write_wav


def story_line(number, text, *, portrait="hero.png", media_id=10):
    return {
        "record_type": "line",
        "line_id": f"reverse1999:story:{number}",
        "speaker": "Hero",
        "voice_character": "Hero",
        "portrait": portrait,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_audio_id": str(1000 + number),
        "source_audio_status": "available",
        "source_event": "play_hero_line",
        "source_bank": "hero_story.bnk",
        "source_media_ids": [media_id],
        "previous_text": f"Before {number}",
        "next_text": f"After {number}",
        "collection_title": "Hero Story",
    }


def write_story(path, lines):
    documents = [
        {
            "record_type": "metadata",
            "schema": "vntts.story-index",
            "schema_version": 1,
            "game": "Synthetic",
            "language": "en",
            "generated_at": "2026-08-18T00:00:00+00:00",
            "line_count": len(lines),
        },
        *lines,
    ]
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in documents),
        encoding="utf-8",
    )
    return path


def write_bank_index(path, audio_root, *, streamed_media_ids=()):
    bank = audio_root / "hero_story.bnk"
    bank.write_bytes(b"synthetic bank")
    stat = bank.stat()
    path.write_text(
        json.dumps(
            {
                "version": index_version,
                "game_audio_directory": str(audio_root),
                "bank_count": 1,
                "banks": [
                    {
                        "filename": bank.name,
                        "path": bank.name,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "embedded_media_ids": [10, 20],
                        "events": [
                            {
                                "event_id": wwise_event_id("play_hero_line"),
                                "media_ids": [10],
                                "streamed_media_ids": list(streamed_media_ids),
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path, bank


def clean_metrics(path):
    return VoiceReferenceMetrics(
        path=str(path),
        duration_seconds=3.2,
        active_duration_seconds=2.88,
        peak_dbfs=-3.0,
        rms_dbfs=-18.0,
        silence_ratio=0.1,
        leading_silence_seconds=0.08,
        trailing_silence_seconds=0.08,
        clipping_ratio=0.0,
        quality_score=100,
        technical_flags=(),
    )


def candidate_fixture(root, lines, media_by_bank):
    story = write_story(root / "story.jsonl", lines)
    audio_root = root / "audio"
    audio_root.mkdir()
    index_path, _bank = write_bank_index(root / "banks.json", audio_root)
    index = json.loads(index_path.read_text())
    entry = index["banks"][0]
    index["banks"] = []
    snapshots = {}
    for bank_name, media in media_by_bank.items():
        bank = audio_root / bank_name
        bank.write_bytes(b"bank " + bank_name.encode())
        routes = {
            wwise_event_id(line["source_event"]): tuple(line["source_media_ids"])
            for line in lines
            if line["source_bank"] == bank_name
        }
        for media_id in media:
            if not any(media_id in values for values in routes.values()):
                routes[media_id] = (media_id,)
        index["banks"].append(
            {
                **entry,
                "filename": bank_name,
                "path": bank_name,
                "size": bank.stat().st_size,
                "mtime_ns": bank.stat().st_mtime_ns,
                "embedded_media_ids": list(media),
                "events": [
                    {"event_id": event_id, "media_ids": list(values), "streamed_media_ids": []}
                    for event_id, values in routes.items()
                ],
            }
        )
        snapshots[bank_name] = BankSnapshot(
            bank, hashlib.sha256(bank.read_bytes()).hexdigest(), media, routes
        )
    index["bank_count"] = len(index["banks"])
    index_path.write_text(json.dumps(index))
    return story, index_path, snapshots


def decode_candidate(data, output, media_id, _decoder, *, bank=None):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    return ImportedReference(
        output,
        media_id,
        hashlib.sha256(data).hexdigest(),
        hashlib.sha256(data).hexdigest(),
        bank,
    )


class StoryVoiceCandidateTest(unittest.TestCase):
    def test_audio_only_admits_bounded_internal_pauses_and_preserves_quality_gates(self):
        sample_rate = 16000

        def paused_audio(active_seconds, pause_seconds, *, amplitude=0.25, edge_seconds=0):
            active_samples = round(active_seconds * sample_rate)
            tone = amplitude * np.sin(2 * np.pi * 220 * np.arange(active_samples) / sample_rate)
            midpoint = active_samples // 2 // 320 * 320
            return np.concatenate(
                (
                    np.zeros(round(edge_seconds * sample_rate)),
                    tone[:midpoint],
                    np.zeros(round(pause_seconds * sample_rate)),
                    tone[midpoint:],
                )
            )

        cases = (
            ("paused", paused_audio(1.6, 1.2), True, ("excessive-silence",)),
            ("half-silent", paused_audio(1.5, 1.5), True, ("excessive-silence",)),
            (
                "mostly-silent",
                np.concatenate((paused_audio(1.52, 1.52), np.zeros(1))),
                False,
                ("excessive-silence",),
            ),
            ("too-little-active", paused_audio(1.48, 1.48), False, ("excessive-silence",)),
            (
                "just-too-little-active",
                paused_audio(1.4999375, 1.48),
                False,
                ("excessive-silence",),
            ),
            ("short", paused_audio(0.84, 0), False, ("too-short",)),
            ("clipped", paused_audio(1.6, 1.2, amplitude=1.2), False, ("clipping",)),
            ("quiet", paused_audio(1.6, 1.2, amplitude=0.02), False, ("too-quiet",)),
            (
                "leading-silence",
                paused_audio(1.6, 0.8, edge_seconds=0.6),
                False,
                ("long-leading-silence",),
            ),
            (
                "trailing-silence",
                paused_audio(1.6, 0.8, edge_seconds=0.6)[::-1],
                False,
                ("long-trailing-silence",),
            ),
        )
        for name, samples, audio_only_pass, expected_flags in cases:
            for policy in ("transcript-bound", "audio-only"):
                with self.subTest(clip=name, policy=policy), TemporaryDirectory() as directory:
                    root = Path(directory)
                    wav = root / "source.wav"
                    write_wav(wav, samples)
                    story, index, snapshots = candidate_fixture(
                        root,
                        [story_line(1, "A phrase, then another phrase.")],
                        {"hero_story.bnk": {10: wav.read_bytes()}},
                    )
                    _path, report = build_story_voice_candidates(
                        story,
                        index,
                        ["Hero"],
                        root / "candidates",
                        decoder="true",
                        bank_loader=lambda _index, bank: snapshots[bank],
                        media_decoder=decode_candidate,
                        reference_admission_policy=policy,
                    )
                    candidate = report["candidates"][0]
                    admitted = audio_only_pass and policy == "audio-only"
                    self.assertEqual(candidate["technical_pass"], admitted)
                    self.assertEqual(
                        report["groups"][0]["recommended_media_ids_for_audition"],
                        [10] if admitted else [],
                    )
                    for flag in expected_flags:
                        self.assertIn(flag, candidate["metrics"]["technical_flags"])
                    self.assertLess(candidate["metrics"]["quality_score"], 100)
                    if name == "half-silent":
                        self.assertEqual(candidate["metrics"]["silence_ratio"], 0.5)
                        self.assertEqual(candidate["metrics"]["active_duration_seconds"], 1.5)
                    elif name == "mostly-silent":
                        self.assertGreater(candidate["metrics"]["silence_ratio"], 0.5)
                    elif name == "just-too-little-active":
                        self.assertEqual(candidate["metrics"]["active_duration_seconds"], 1.4999375)

    def test_audio_only_admits_conflicting_texts_without_penalty_or_losing_provenance(self):
        for policy in ("transcript-bound", "audio-only"):
            with self.subTest(policy=policy), TemporaryDirectory() as directory:
                root = Path(directory)
                lines = [
                    story_line(1, "First source text.", media_id=10),
                    {
                        **story_line(2, "Different source text.", media_id=20),
                        "source_event": "play_second",
                    },
                    {
                        **story_line(3, "Clean control.", media_id=30),
                        "source_event": "play_control",
                    },
                ]
                story, index, snapshots = candidate_fixture(
                    root,
                    lines,
                    {"hero_story.bnk": {10: b"same", 20: b"same", 30: b"clean"}},
                )
                _path, report = build_story_voice_candidates(
                    story,
                    index,
                    ["Hero"],
                    root / "candidates",
                    decoder="true",
                    bank_loader=lambda _index, bank: snapshots[bank],
                    media_decoder=decode_candidate,
                    analyzer=clean_metrics,
                    reference_admission_policy=policy,
                )
                self.assertEqual(report["reference_admission_policy"], policy)
                self.assertEqual(
                    report["groups"][0]["recommended_media_ids_for_audition"],
                    [10, 20, 30] if policy == "audio-only" else [30],
                )
                for candidate in report["candidates"][:2]:
                    self.assertTrue(candidate["transcript_conflict"])
                    self.assertFalse(candidate["speaker_identity_conflict"])
                    self.assertEqual(len(candidate["source_lines"]), 1)
                    self.assertEqual(
                        {line["text"] for line in candidate["reference_source_lines"]},
                        {"First source text.", "Different source text."},
                    )

    def test_audio_only_rejects_known_route_owners_and_decoded_speaker_ambiguity(self):
        for conflict in ("unrequested-character", "portrait", "cross-bank-decoded"):
            with self.subTest(conflict=conflict), TemporaryDirectory() as directory:
                root = Path(directory)
                second = story_line(2, "Same text.")
                roles = ["Hero"]
                media = {"hero_story.bnk": {10: b"same"}}
                if conflict == "portrait":
                    second["portrait"] = "other.png"
                else:
                    second["voice_character"] = "Other"
                    second["speaker"] = "Other"
                if conflict == "cross-bank-decoded":
                    second["source_bank"] = "other_story.bnk"
                    second["source_media_ids"] = [20]
                    media["other_story.bnk"] = {20: b"same"}
                    roles.append("Other")
                story, index, snapshots = candidate_fixture(
                    root, [story_line(1, "Same text."), second], media
                )
                _path, report = build_story_voice_candidates(
                    story,
                    index,
                    roles,
                    root / "candidates",
                    decoder="true",
                    bank_loader=lambda _index, bank: snapshots[bank],
                    media_decoder=decode_candidate,
                    analyzer=clean_metrics,
                    reference_admission_policy="audio-only",
                )
                self.assertTrue(all(c["speaker_identity_conflict"] for c in report["candidates"]))
                self.assertFalse(any(c["transcript_conflict"] for c in report["candidates"]))
                self.assertFalse(
                    any(group["recommended_media_ids_for_audition"] for group in report["groups"])
                )
                if conflict == "unrequested-character":
                    self.assertEqual(report["candidate_count"], 1)

    def test_audio_only_manual_too_long_admission_still_requires_one_speaker(self):
        candidate = {
            "source_bank": "hero3146_mainvoc.bnk",
            "source_lines": [{"line_id": "playable-voice:3146:1314605:4"}],
            "metrics": {"technical_flags": ["too-long"]},
            "transcript_conflict": True,
            "speaker_identity_conflict": False,
        }
        self.assertFalse(is_manual_playable_too_long_candidate(candidate))
        self.assertTrue(is_manual_playable_too_long_candidate(candidate, "audio-only"))
        self.assertFalse(
            is_manual_playable_too_long_candidate(
                {**candidate, "speaker_identity_conflict": True}, "audio-only"
            )
        )

    def test_inventory_captures_coverage_and_other_bank_owners_in_one_json_pass(self):
        with TemporaryDirectory() as directory:
            story = write_story(
                Path(directory) / "story.jsonl",
                [
                    story_line(1, "Hero line."),
                    {**story_line(2, "Missing line."), "source_audio_status": "absent"},
                    {
                        **story_line(3, "Not speakable."),
                        "source_audio_status": "absent",
                        "speakable": False,
                    },
                    {**story_line(4, "Other owner."), "voice_character": "Other"},
                ],
            )
            expected_rows = story.read_text().splitlines()
            with patch(
                "r1999extractor.story_voice_candidates.json.loads", wraps=json.loads
            ) as loads:
                lines, digest, characters, portraits, owners, media_owners = (
                    _collect_story_voice_inventory(story, iter(["Hero"]))
                )
            self.assertEqual([call.args[0] for call in loads.call_args_list], expected_rows)
            self.assertEqual(digest, hashlib.sha256(story.read_bytes()).hexdigest())
            self.assertEqual([line.line_id for line in lines], ["reverse1999:story:1"])
            self.assertEqual((characters, portraits), affected_story_line_counts(story, ["Hero"]))
            self.assertEqual(characters, {"Hero": 1})
            self.assertEqual(
                owners, {"hero_story.bnk": {("hero", "hero.png"), ("other", "hero.png")}}
            )
            self.assertEqual(
                media_owners,
                {("hero_story.bnk", 10): {("hero", "hero.png"), ("other", "hero.png")}},
            )

    def test_concurrent_preparation_limits_consumption_to_four_batches_of_eight(self):
        started = Barrier(4)
        release = Event()
        consumed = []

        def items():
            for number in range(12):
                batch = tuple(range(number * 8, (number + 1) * 8))
                consumed.extend(batch)
                yield batch

        def prepare(batch):
            number = batch[0] // 8
            if number < 4:
                started.wait(timeout=3)
                if number != 1:
                    self.assertTrue(release.wait(3))
            return batch

        prepared = _prepare_references_concurrently(prepare, items())
        try:
            self.assertEqual(next(prepared), tuple(range(8, 16)))
            self.assertEqual(consumed, list(range(32)))
            release.set()
            self.assertCountEqual(
                [number for batch in prepared for number in batch], [*range(8), *range(16, 96)]
            )
        finally:
            release.set()
            prepared.close()

    def test_concurrent_preparation_refills_workers_while_first_item_is_blocked(self):
        first_started = Event()
        release_first = Event()

        def prepare(number):
            if number == 0:
                first_started.set()
                self.assertTrue(release_first.wait(3))
            return number

        prepared = _prepare_references_concurrently(prepare, range(12))
        try:
            self.assertCountEqual([next(prepared) for _ in range(11)], range(1, 12))
            self.assertTrue(first_started.is_set())
            release_first.set()
            self.assertEqual(list(prepared), [0])
        finally:
            release_first.set()
            prepared.close()

    def test_concurrent_failure_waits_for_workers_before_cleanup(self):
        started = Barrier(4)
        failed = Event()
        finished = []

        def prepare(number):
            if number < 4:
                started.wait(timeout=3)
            if number == 0:
                failed.set()
                raise RuntimeError("decoder failed")
            self.assertTrue(failed.wait(3))
            finished.append(number)
            return number

        with self.assertRaisesRegex(RuntimeError, "decoder failed"):
            list(_prepare_references_concurrently(prepare, range(12)))
        self.assertTrue({1, 2, 3} <= set(finished))

    def test_concurrent_generator_close_waits_for_running_workers(self):
        started = Barrier(4)
        release = Event()
        finished = []

        def prepare(number):
            started.wait(timeout=3)
            if number:
                self.assertTrue(release.wait(3))
                finished.append(number)
            return number

        prepared = _prepare_references_concurrently(prepare, range(12))
        self.assertEqual(next(prepared), 0)
        release.set()
        prepared.close()
        self.assertCountEqual(finished, [1, 2, 3])

    def test_candidate_report_restores_identity_order_after_out_of_order_completion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_ids = [2, 10, 20, *range(30, 37)]
            expected_order = sorted(media_ids, key=str)
            story = write_story(
                root / "story.jsonl",
                [{**story_line(1, "Hero."), "source_media_ids": media_ids}],
            )
            audio = root / "audio"
            audio.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio)
            document = json.loads(bank_index.read_text())
            document["banks"][0]["embedded_media_ids"] = media_ids
            document["banks"][0]["events"][0]["media_ids"] = media_ids
            bank_index.write_text(json.dumps(document))
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={media_id: str(media_id).encode() for media_id in media_ids},
                routes={wwise_event_id("play_hero_line"): tuple(media_ids)},
            )

            def decode(data, output, media_id, _decoder, *, bank=None):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                digest = hashlib.sha256(data).hexdigest()
                return ImportedReference(output, media_id, digest, digest, bank)

            def complete_in_reverse(prepare, items):
                return iter([prepare(item) for item in reversed(list(items))])

            def decode_batch(references, decoder):
                return [
                    decode(data, output, media_id, decoder, bank=bank)
                    for data, output, media_id, bank in references
                ]

            with (
                patch("r1999extractor.story_voice_candidates.decode_reference_data", decode),
                patch(
                    "r1999extractor.story_voice_candidates.analyze_voice_reference", clean_metrics
                ),
                patch(
                    "r1999extractor.story_voice_candidates._prepare_references_concurrently",
                    side_effect=complete_in_reverse,
                ) as workers,
                patch(
                    "r1999extractor.story_voice_candidates.decode_reference_batch",
                    side_effect=decode_batch,
                ) as batch_decoder,
            ):
                report_path, report = build_story_voice_candidates(
                    story,
                    bank_index,
                    ["Hero"],
                    root / "candidates",
                    decoder="true",
                    bank_loader=lambda _index, _bank: snapshot,
                    media_decoder=decode,
                    analyzer=clean_metrics,
                )
            self.assertEqual(workers.call_count, 1)
            self.assertEqual([len(call.args[0]) for call in batch_decoder.call_args_list], [2, 8])
            self.assertEqual([item["media_id"] for item in report["candidates"]], expected_order)
            self.assertEqual(
                [item["media_id"] for item in json.loads(report_path.read_text())["candidates"]],
                expected_order,
            )

    def test_batch_failure_removes_partial_candidate_staging(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(root / "story.jsonl", [story_line(1, "Hero.")])
            audio = root / "audio"
            audio.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio)
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"voice"},
                routes={wwise_event_id("play_hero_line"): (10,)},
            )

            def fail_batch(references, _decoder):
                output = references[0][1]
                output.parent.mkdir(parents=True)
                output.write_bytes(b"partial reference")
                raise RuntimeError("batch failed")

            with patch(
                "r1999extractor.story_voice_candidates.decode_reference_batch",
                side_effect=fail_batch,
            ):
                with self.assertRaisesRegex(RuntimeError, "batch failed"):
                    build_story_voice_candidates(
                        story,
                        bank_index,
                        ["Hero"],
                        root / "candidates",
                        decoder="true",
                        bank_loader=lambda _index, _bank: snapshot,
                    )
            self.assertFalse((root / "candidates").exists())
            self.assertFalse(list(root.glob(".*.staging-*")))

    def test_default_workers_are_eligible_but_custom_callbacks_keep_main_thread_and_guards(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(root / "story.jsonl", [story_line(1, "Hero.")])
            audio = root / "audio"
            audio.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio)
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"voice"},
                routes={wwise_event_id("play_hero_line"): (10,)},
            )
            kwargs = {"decoder": "true", "bank_loader": lambda _index, _bank: snapshot}
            caller = get_ident()

            def decode(data, output, media_id, _decoder, *, bank=None):
                self.assertEqual(get_ident(), caller)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(data).hexdigest(),
                    hashlib.sha256(data).hexdigest(),
                    bank,
                )

            with patch(
                "r1999extractor.story_voice_candidates._prepare_references_concurrently",
                side_effect=RuntimeError("worker marker"),
            ) as workers:
                with self.assertRaisesRegex(RuntimeError, "worker marker"):
                    build_story_voice_candidates(
                        story, bank_index, ["Hero"], root / "default", **kwargs
                    )
                self.assertEqual(workers.call_count, 1)
                self.assertFalse((root / "default").exists())
                build_story_voice_candidates(
                    story,
                    bank_index,
                    ["Hero"],
                    root / "custom",
                    media_decoder=decode,
                    analyzer=clean_metrics,
                    **kwargs,
                )
                self.assertEqual(workers.call_count, 1)

                write_story(
                    story,
                    [story_line(1, "Hero."), story_line(2, "Hero smile.", portrait="hero.jpg")],
                )
                with (
                    patch("r1999extractor.story_voice_candidates.decode_reference_data", decode),
                    patch(
                        "r1999extractor.story_voice_candidates.analyze_voice_reference",
                        clean_metrics,
                    ),
                ):
                    _path, report = build_story_voice_candidates(
                        story,
                        bank_index,
                        ["Hero"],
                        root / "collision",
                        media_decoder=decode,
                        analyzer=clean_metrics,
                        **kwargs,
                    )
                self.assertEqual(workers.call_count, 1)
                self.assertEqual(len(report["candidates"]), 2)
                self.assertEqual(
                    report["candidates"][0]["reference"], report["candidates"][1]["reference"]
                )

                def mutate_story(*args, **kw):
                    result = decode(*args, **kw)
                    story.write_text("changed")
                    return result

                with self.assertRaisesRegex(StoryVoiceCandidateError, "Story index changed"):
                    build_story_voice_candidates(
                        story,
                        bank_index,
                        ["Hero"],
                        root / "changed",
                        media_decoder=mutate_story,
                        analyzer=clean_metrics,
                        **kwargs,
                    )
            self.assertFalse((root / "changed").exists())
            self.assertFalse(list(root.glob(".*.staging-*")))

    def test_batch_rejections_preserve_evidence_order_progress_and_good_references(self):
        for failure_stage in ("decode", "analyze"):
            with self.subTest(failure_stage=failure_stage), TemporaryDirectory() as directory:
                root = Path(directory)
                media = {number: str(number).encode() for number in range(10, 20)}
                lines = [
                    {
                        **story_line(number, "Shared text.", media_id=number),
                        "source_event": f"play_line_{number}",
                    }
                    for number in media
                ]
                story, index, snapshots = candidate_fixture(root, lines, {"hero_story.bnk": media})
                rejected = {10, 13, 19}
                progress = []
                single_calls = []

                def decode(data, output, media_id, decoder, *, bank=None):
                    self.assertFalse(output.exists(), "Partial batch output survived retry")
                    single_calls.append(media_id)
                    result = decode_candidate(data, output, media_id, decoder, bank=bank)
                    if failure_stage == "decode" and media_id in rejected:
                        raise UnusableVoiceReferenceError("WAV contains no audio")
                    return result

                def analyze(path):
                    if failure_stage == "analyze" and int(path.read_bytes()) in rejected:
                        raise UnusableVoiceReferenceError("WAV contains no speech-level audio")
                    return clean_metrics(path)

                def decode_batch(references, decoder):
                    return [
                        decode(data, output, media_id, decoder, bank=bank)
                        for data, output, media_id, bank in references
                    ]

                def reverse_batches(prepare, batches):
                    return iter([prepare(batch) for batch in reversed(list(batches))])

                with (
                    patch("r1999extractor.story_voice_candidates.decode_reference_data", decode),
                    patch("r1999extractor.story_voice_candidates.analyze_voice_reference", analyze),
                    patch(
                        "r1999extractor.story_voice_candidates.decode_reference_batch",
                        side_effect=decode_batch,
                    ) as batch_decoder,
                    patch(
                        "r1999extractor.story_voice_candidates._prepare_references_concurrently",
                        side_effect=reverse_batches,
                    ),
                ):
                    report_path, report = build_story_voice_candidates(
                        story,
                        index,
                        ["Hero"],
                        root / "candidates",
                        decoder="true",
                        bank_loader=lambda _index, bank: snapshots[bank],
                        media_decoder=decode,
                        analyzer=analyze,
                        progress=progress.append,
                    )
                self.assertEqual(batch_decoder.call_count, 2)
                self.assertEqual(
                    [row["media_id"] for row in report["candidates"]], sorted(set(media) - rejected)
                )
                self.assertEqual(
                    [row["media_id"] for row in report["rejected_candidates"]], sorted(rejected)
                )
                for row in report["rejected_candidates"]:
                    self.assertEqual(
                        row["source_sha256"], hashlib.sha256(media[row["media_id"]]).hexdigest()
                    )
                    self.assertEqual(len(row["source_lines"]), 1)
                    self.assertEqual(row["rejection_reason"], "unusable_audio")
                self.assertEqual(len(list(report_path.parent.rglob("*.wav"))), 7)
                self.assertEqual([item["processed"] for item in progress], [*range(11), 10])
                self.assertEqual(progress[-1]["state"], "publishing")
                self.assertTrue(all(item["total"] == 10 for item in progress))
                self.assertEqual(len(single_calls), 13 if failure_stage == "decode" else 10)

    def test_keeps_rejected_audio_evidence_and_continues_other_characters(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lines = [
                story_line(1, "Silent hero."),
                {
                    **story_line(2, "Other's usable line.", media_id=30),
                    "voice_character": "Other",
                    "speaker": "Other",
                    "source_bank": "other_story.bnk",
                    "source_event": "play_other_line",
                },
            ]
            story, index, snapshots = candidate_fixture(
                root,
                lines,
                {
                    "hero_story.bnk": {10: b"silent", 20: b"silent unlinked"},
                    "other_story.bnk": {30: b"speech"},
                },
            )

            def decode(data, *args, **kwargs):
                if data.startswith(b"silent"):
                    raise UnusableVoiceReferenceError("WAV contains no speech-level audio")
                return decode_candidate(data, *args, **kwargs)

            report_path, report = build_story_voice_candidates(
                story,
                index,
                ["Hero", "Other"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, bank: snapshots[bank],
                media_decoder=decode,
                analyzer=clean_metrics,
                include_unlinked_bank_media=True,
            )
            self.assertEqual(report["candidate_count"], 1)
            self.assertEqual(report["candidates"][0]["character"], "Other")
            self.assertEqual(report["rejected_candidate_count"], 2)
            self.assertEqual(report["outcome"], "candidates_prepared")
            by_media = {item["media_id"]: item for item in report["rejected_candidates"]}
            self.assertEqual(by_media[10]["source_lines"][0]["line_id"], lines[0]["line_id"])
            self.assertEqual(by_media[10]["source_sha256"], hashlib.sha256(b"silent").hexdigest())
            self.assertEqual(by_media[20]["source_lines"], [])
            self.assertEqual(by_media[20]["candidate_origin"], "exact_bank_unrouted_media")
            self.assertEqual(by_media[20]["rejection_reason"], "unusable_audio")
            self.assertIn("no speech-level audio", by_media[20]["error"])
            self.assertNotIn("reference", by_media[20])
            self.assertEqual(json.loads(report_path.read_text()), json.loads(json.dumps(report)))

    def test_all_unusable_audio_still_publishes_diagnostics_without_a_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, index, snapshots = candidate_fixture(
                root, [story_line(1, "Silent.")], {"hero_story.bnk": {10: b"silent"}}
            )

            def analyze(_path):
                raise UnusableVoiceReferenceError("WAV contains no audio")

            report_path, report = build_story_voice_candidates(
                story,
                index,
                ["Hero"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, bank: snapshots[bank],
                media_decoder=decode_candidate,
                analyzer=analyze,
            )
            self.assertTrue(report_path.is_file())
            self.assertEqual(report["outcome"], "no_usable_candidates")
            self.assertEqual(report["candidates"], [])
            self.assertEqual(report["groups"], [])
            self.assertEqual(report["rejected_candidate_count"], 1)
            self.assertFalse((report_path.parent / "manifest.json").exists())
            self.assertEqual(list(report_path.parent.rglob("*.wav")), [])

    def test_rejected_audio_does_not_bypass_final_source_integrity_checks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, index, snapshots = candidate_fixture(
                root, [story_line(1, "Silent.")], {"hero_story.bnk": {10: b"silent"}}
            )

            def decode(*_args, **_kwargs):
                snapshots["hero_story.bnk"].path.write_bytes(b"bank changed")
                raise UnusableVoiceReferenceError("WAV contains no audio")

            with self.assertRaisesRegex(StoryVoiceCandidateError, "bank changed"):
                build_story_voice_candidates(
                    story,
                    index,
                    ["Hero"],
                    root / "candidates",
                    decoder="true",
                    bank_loader=lambda _index, bank: snapshots[bank],
                    media_decoder=decode,
                    analyzer=clean_metrics,
                )
            self.assertFalse((root / "candidates").exists())

    def test_same_wave_conflicts_also_cross_requested_character_identities(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lines = [
                story_line(1, "Hero transcript."),
                {
                    **story_line(2, "Other transcript.", media_id=20),
                    "voice_character": "Other",
                    "speaker": "Other",
                    "source_event": "play_other_line",
                },
            ]
            story, index, snapshots = candidate_fixture(
                root, lines, {"hero_story.bnk": {10: b"same wave", 20: b"same wave"}}
            )
            _path, report = build_story_voice_candidates(
                story,
                index,
                ["Hero", "Other"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, bank: snapshots[bank],
                media_decoder=decode_candidate,
                analyzer=clean_metrics,
            )
            self.assertTrue(all(item["transcript_conflict"] for item in report["candidates"]))
            self.assertTrue(
                all(not group["recommended_media_ids_for_audition"] for group in report["groups"])
            )

    def test_does_not_downgrade_operational_or_invalid_audio_failures(self):
        for error in (
            OSError("disk failed"),
            RuntimeError("decoder unavailable"),
            AudioConversionError("decoder failed"),
            VoiceReferenceQualityError("invalid WAV"),
        ):
            with self.subTest(error=error), TemporaryDirectory() as directory:
                root = Path(directory)
                story, index, snapshots = candidate_fixture(
                    root, [story_line(1, "Line.")], {"hero_story.bnk": {10: b"audio"}}
                )

                def decode(*_args, **_kwargs):
                    raise error

                with self.assertRaises(type(error)):
                    build_story_voice_candidates(
                        story,
                        index,
                        ["Hero"],
                        root / "candidates",
                        decoder="true",
                        bank_loader=lambda _index, bank: snapshots[bank],
                        media_decoder=decode,
                        analyzer=clean_metrics,
                    )
                self.assertFalse((root / "candidates").exists())

    def test_reconciles_same_wave_transcripts_across_portraits_banks_and_characters(self):
        for cross_bank in (False, True):
            for same_text in (False, True):
                with (
                    self.subTest(cross_bank=cross_bank, same_text=same_text),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    lines = [
                        story_line(1, "First transcript.", portrait="adult.png"),
                        {
                            **story_line(
                                2,
                                "First transcript." if same_text else "Conflicting transcript.",
                                portrait="young.png",
                                media_id=20,
                            ),
                            "source_bank": "other_story.bnk" if cross_bank else "hero_story.bnk",
                            "source_event": "play_second_line",
                        },
                        {
                            **story_line(3, "Clean control.", media_id=30),
                            "voice_character": "Other",
                            "speaker": "Other",
                            "source_event": "play_other_line",
                        },
                    ]
                    media = {"hero_story.bnk": {10: b"same wave", 30: b"different wave"}}
                    media.setdefault(lines[1]["source_bank"], {})[20] = b"same wave"
                    story, index, snapshots = candidate_fixture(root, lines, media)
                    _path, report = build_story_voice_candidates(
                        story,
                        index,
                        ["Hero", "Other"],
                        root / "candidates",
                        decoder="true",
                        bank_loader=lambda _index, bank: snapshots[bank],
                        media_decoder=decode_candidate,
                        analyzer=clean_metrics,
                    )
                    duplicates = [
                        item for item in report["candidates"] if item["character"] == "Hero"
                    ]
                    for item in duplicates:
                        self.assertEqual(item["transcript_conflict"], not same_text)
                        self.assertTrue(item["technical_pass"])
                        self.assertEqual(item["metrics"]["technical_flags"], ())
                        self.assertEqual(len(item["source_lines"]), 1)
                        self.assertEqual(len(item["reference_source_lines"]), 2)
                    control = next(
                        item for item in report["candidates"] if item["character"] == "Other"
                    )
                    self.assertFalse(control["transcript_conflict"])
                    recommendations = [
                        media_id
                        for group in report["groups"]
                        for media_id in group["recommended_media_ids_for_audition"]
                    ]
                    self.assertEqual(set(recommendations), {10, 20, 30} if same_text else {30})

    def test_manual_picker_accepts_only_playable_main_voice_with_too_long_flag(self):
        candidate = {
            "source_bank": "hero3146_mainvoc.bnk",
            "source_lines": [{"line_id": "playable-voice:3146:1314605:4"}],
            "metrics": {"technical_flags": ["too-long"]},
            "transcript_conflict": False,
        }

        self.assertTrue(is_manual_playable_too_long_candidate(candidate))
        self.assertTrue(
            is_manual_playable_too_long_candidate(
                {**candidate, "metrics": {"technical_flags": ["too-long", "excessive-silence"]}}
            )
        )
        for field, value in (
            ("transcript_conflict", True),
            ("source_bank", "hero3146_combat.bnk"),
            ("metrics", {"technical_flags": ["too-long", "music-or-sfx"]}),
            ("metrics", {"technical_flags": []}),
        ):
            rejected = {**candidate, field: value}
            self.assertFalse(is_manual_playable_too_long_candidate(rejected))

    def test_collects_only_exact_installed_role_and_validates_text_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(
                root / "story.jsonl",
                [
                    story_line(1, "Exact hero line."),
                    {**story_line(2, "Unavailable."), "source_audio_status": "absent"},
                    {**story_line(3, "Other."), "voice_character": "Other"},
                ],
            )
            expected_digest = hashlib.sha256(story.read_bytes()).hexdigest()

            lines, digest = collect_story_voice_lines(story, ["Hero"])

        self.assertEqual([line.line_id for line in lines], ["reverse1999:story:1"])
        self.assertEqual(digest, expected_digest)

    def test_missing_exact_role_fails_instead_of_borrowing_another_voice(self):
        with TemporaryDirectory() as directory:
            story = write_story(Path(directory) / "story.jsonl", [story_line(1, "Hero")])

            with self.assertRaisesRegex(StoryVoiceCandidateError, "Missing"):
                collect_story_voice_lines(story, ["Missing"])

    def test_playable_speech_filter_excludes_combat_without_losing_story_variants(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lines = [
                {
                    **story_line(1, "Story voice remains available."),
                    "source_audio_id": "1001",
                    "source_event": "play_story",
                    "source_bank": "hero_story.bnk",
                    "source_media_ids": [10],
                },
                {
                    **story_line(2, "Playable greeting remains available."),
                    "line_id": "playable-voice:3141:100:0",
                    "source_audio_id": "1002",
                    "source_event": "play_main",
                    "source_bank": "mianvoc_hero3141.bnk",
                    "source_media_ids": [11],
                },
                {
                    **story_line(3, "Combat clip is not a reference."),
                    "line_id": "playable-voice:3141:101:1",
                    "source_audio_id": "1003",
                    "source_event": "play_combat",
                    "source_bank": "hero3141_combat.bnk",
                    "source_media_ids": [12],
                },
            ]
            story = write_story(root / "story.jsonl", lines)
            snapshots = {}
            banks = []
            for line in lines:
                bank = root / line["source_bank"]
                payload = line["source_audio_id"].encode()
                bank.write_bytes(payload)
                stat = bank.stat()
                banks.append(
                    {
                        "filename": bank.name,
                        "path": bank.name,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "embedded_media_ids": line["source_media_ids"],
                        "events": [
                            {
                                "event_id": wwise_event_id(line["source_event"]),
                                "media_ids": line["source_media_ids"],
                            }
                        ],
                    }
                )
                snapshots[bank.name] = BankSnapshot(
                    path=bank,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    media={line["source_media_ids"][0]: payload},
                    routes={wwise_event_id(line["source_event"]): tuple(line["source_media_ids"])},
                )
            bank_index = root / "banks.json"
            bank_index.write_text(
                json.dumps(
                    {
                        "version": index_version,
                        "game_audio_directory": str(root),
                        "bank_count": len(banks),
                        "banks": banks,
                    }
                ),
                encoding="utf-8",
            )
            decoded_banks = []

            def decode(data, output, media_id, _decoder, *, bank=None):
                decoded_banks.append(bank)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(data).hexdigest(),
                    hashlib.sha256(data).hexdigest(),
                    bank,
                )

            _path, report = build_story_voice_candidates(
                story,
                bank_index,
                ["Hero"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, name: snapshots[name],
                media_decoder=decode,
                analyzer=clean_metrics,
                playable_speech_only=True,
            )

        self.assertEqual(
            {
                line["line_id"]
                for candidate in report["candidates"]
                for line in candidate["source_lines"]
            },
            {"reverse1999:story:1", "playable-voice:3141:100:0"},
        )
        self.assertNotIn("hero3141_combat.bnk", decoded_banks)

    def test_counts_only_missing_source_speakable_lines_by_exact_portrait(self):
        with TemporaryDirectory() as directory:
            story = write_story(
                Path(directory) / "story.jsonl",
                [
                    story_line(1, "Installed."),
                    {
                        **story_line(2, "Missing adult."),
                        "source_audio_status": "absent",
                    },
                    {
                        **story_line(3, "Missing young.", portrait="young.png"),
                        "source_audio_status": "unavailable",
                    },
                    {
                        **story_line(4, "Not speakable."),
                        "source_audio_status": "absent",
                        "speakable": False,
                    },
                ],
            )

            characters, portraits = affected_story_line_counts(story, ["Hero"])

        self.assertEqual(characters["Hero"], 2)
        self.assertEqual(portraits[("Hero", "hero.png")], 1)
        self.assertEqual(portraits[("Hero", "young.png")], 1)

    def test_normalized_quoted_display_variant_keeps_requested_role_identity(self):
        with TemporaryDirectory() as directory:
            line = story_line(1, "Exact role line.")
            line["speaker"] = '"Mrs. Owen"'
            line["voice_character"] = '"Mrs. Owen"'
            story = write_story(Path(directory) / "story.jsonl", [line])

            lines, _digest = collect_story_voice_lines(story, ["Mrs. Owen"])

        self.assertEqual(lines[0].character, "Mrs. Owen")
        self.assertEqual(lines[0].speaker, '"Mrs. Owen"')

    def test_builds_grouped_checksum_bound_audition_without_manifest_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(
                root / "story.jsonl",
                [
                    story_line(1, "First transcript.", portrait="adult.png"),
                    story_line(2, "Second transcript.", portrait="adult.png"),
                    story_line(3, "Young transcript.", portrait="young.png"),
                ],
            )
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio_root)
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"exact media"},
                routes={wwise_event_id("play_hero_line"): (10,)},
            )

            def decode(data, output, media_id, _decoder, *, bank=None):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"normalized wav")
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(data).hexdigest(),
                    hashlib.sha256(output.read_bytes()).hexdigest(),
                    bank,
                )

            events = []
            report_path, report = build_story_voice_candidates(
                story,
                bank_index,
                ["Hero"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, _filename: snapshot,
                media_decoder=decode,
                analyzer=clean_metrics,
                progress=events.append,
            )
            self.assertEqual(
                [
                    (event["stage"], event["processed"], event["total"], event["state"])
                    for event in events
                ],
                [
                    ("references", 0, 2, "analyzing"),
                    ("references", 1, 2, "analyzing"),
                    ("references", 2, 2, "analyzing"),
                    ("references", 2, 2, "publishing"),
                ],
            )

            def fail_analysis(_path):
                raise RuntimeError("analysis failed")

            failed_events = []
            with self.assertRaisesRegex(RuntimeError, "analysis failed"):
                build_story_voice_candidates(
                    story,
                    bank_index,
                    ["Hero"],
                    root / "failed",
                    decoder="true",
                    bank_loader=lambda _index, _filename: snapshot,
                    media_decoder=decode,
                    analyzer=fail_analysis,
                    progress=failed_events.append,
                )
            self.assertEqual([event["processed"] for event in failed_events], [0])
            self.assertFalse((root / "failed").exists())
            reference_digest = hashlib.sha256(
                (report_path.parent / report["candidates"][0]["reference"]).read_bytes()
            ).hexdigest()
            manifest_exists = (report_path.parent / "manifest.json").exists()

        self.assertEqual(report["source_line_count"], 3)
        self.assertEqual(report["bank_inventory_scope"], "story_routed_only")
        self.assertEqual(report["group_count"], 2)
        self.assertEqual(report["candidate_count"], 2)
        self.assertTrue(report["candidates"][0]["transcript_conflict"])
        self.assertTrue(report["candidates"][0]["manual_content_review_required"])
        self.assertEqual(report["candidates"][0]["affected_character_line_count"], 0)
        self.assertEqual(report["candidates"][0]["source_lines"][0]["previous_text"], "Before 1")
        self.assertEqual(
            report["candidates"][0]["metrics"]["path"],
            report["candidates"][0]["reference"],
        )
        self.assertEqual(reference_digest, report["candidates"][0]["reference_sha256"])
        self.assertFalse(manifest_exists)

    def test_uses_full_external_media_for_a_streamed_route(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(root / "story.jsonl", [story_line(1, "Exact transcript.")])
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, bank = write_bank_index(
                root / "banks.json", audio_root, streamed_media_ids=(10,)
            )
            (root / "Media").mkdir()
            (root / "Media/10.wem").write_bytes(b"full external media")
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"embedded prefetch"},
                routes={wwise_event_id("play_hero_line"): (10,)},
                streamed_routes={wwise_event_id("play_hero_line"): (10,)},
            )
            decoded = []

            def decode(data, output, media_id, _decoder, *, bank=None):
                decoded.append(data)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"normalized wav")
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(data).hexdigest(),
                    hashlib.sha256(output.read_bytes()).hexdigest(),
                    bank,
                )

            _report_path, report = build_story_voice_candidates(
                story,
                bank_index,
                ["Hero"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, _filename: snapshot,
                media_decoder=decode,
                analyzer=clean_metrics,
            )

            def decode_batch(references, decoder):
                return [
                    decode(data, output, media_id, decoder, bank=bank)
                    for data, output, media_id, bank in references
                ]

            with (
                patch("r1999extractor.story_voice_candidates.decode_reference_data", decode),
                patch(
                    "r1999extractor.story_voice_candidates.analyze_voice_reference", clean_metrics
                ),
                patch(
                    "r1999extractor.story_voice_candidates.decode_reference_batch",
                    side_effect=decode_batch,
                ) as batch_decoder,
            ):
                _batch_path, batch_report = build_story_voice_candidates(
                    story,
                    bank_index,
                    ["Hero"],
                    root / "batch-candidates",
                    decoder="true",
                    bank_loader=lambda _index, _filename: snapshot,
                    media_decoder=decode,
                    analyzer=clean_metrics,
                )
            self.assertEqual(batch_decoder.call_count, 1)
            self.assertEqual(batch_report["candidates"], report["candidates"])

        self.assertEqual(decoded, [b"full external media", b"full external media"])
        self.assertEqual(
            report["candidates"][0]["source_sha256"],
            hashlib.sha256(b"full external media").hexdigest(),
        )

    def test_does_not_prepare_a_candidate_for_an_unavailable_full_stream(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(
                root / "story.jsonl",
                [
                    {
                        **story_line(1, "Unavailable full audio."),
                        "source_audio_status": "unavailable",
                    }
                ],
            )
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, _bank = write_bank_index(
                root / "banks.json", audio_root, streamed_media_ids=(10,)
            )

            with self.assertRaisesRegex(StoryVoiceCandidateError, "No installed same-speaker"):
                build_story_voice_candidates(
                    story,
                    bank_index,
                    ["Hero"],
                    root / "candidates",
                    decoder="true",
                )

    def test_rejects_a_media_id_with_conflicting_embedded_and_streamed_routes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            streamed = {
                **story_line(2, "Streamed route."),
                "source_event": "play_hero_stream",
            }
            story = write_story(root / "story.jsonl", [story_line(1, "Embedded route."), streamed])
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio_root)
            document = json.loads(bank_index.read_text(encoding="utf-8"))
            document["banks"][0]["events"].append(
                {
                    "event_id": wwise_event_id("play_hero_stream"),
                    "media_ids": [10],
                    "streamed_media_ids": [10],
                }
            )
            bank_index.write_text(json.dumps(document), encoding="utf-8")
            (root / "Media").mkdir()
            (root / "Media/10.wem").write_bytes(b"full external media")
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"embedded media"},
                routes={
                    wwise_event_id("play_hero_line"): (10,),
                    wwise_event_id("play_hero_stream"): (10,),
                },
                streamed_routes={wwise_event_id("play_hero_stream"): (10,)},
            )

            with self.assertRaisesRegex(StoryVoiceCandidateError, "Conflicting embedded"):
                build_story_voice_candidates(
                    story,
                    bank_index,
                    ["Hero"],
                    root / "candidates",
                    decoder="true",
                    bank_loader=lambda _index, _filename: snapshot,
                )

    def test_include_all_bank_media_adds_unrouted_candidates_without_transcripts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(
                root / "story.jsonl",
                [story_line(1, "Routed exact line.", portrait="adult.png")],
            )
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio_root)
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"routed media", 20: b"unrouted media"},
                routes={
                    wwise_event_id("play_hero_line"): (10,),
                    2020: (20,),
                },
            )

            def decode(data, output, media_id, _decoder, *, bank=None):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"normalized " + data)
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(data).hexdigest(),
                    hashlib.sha256(output.read_bytes()).hexdigest(),
                    bank,
                )

            _report_path, report = build_story_voice_candidates(
                story,
                bank_index,
                ["Hero"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, _filename: snapshot,
                media_decoder=decode,
                analyzer=clean_metrics,
                include_all_bank_media=True,
            )

        by_media = {candidate["media_id"]: candidate for candidate in report["candidates"]}
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["bank_inventory_scope"], "complete_exact_bank")
        self.assertEqual(set(by_media), {10, 20})
        self.assertEqual(by_media[10]["candidate_origin"], "story_line_route")
        self.assertEqual(len(by_media[10]["source_lines"]), 1)
        self.assertEqual(by_media[20]["candidate_origin"], "exact_bank_unrouted_media")
        self.assertEqual(by_media[20]["source_event_ids"], [2020])
        self.assertEqual(by_media[20]["source_lines"], [])

    def test_player_mode_adds_only_safe_unrouted_bank_media(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(root / "story.jsonl", [story_line(1, "Exact line.")])
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio_root)
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"routed", 20: b"safe", 30: b"unrouted"},
                routes={
                    wwise_event_id("play_hero_line"): (10,),
                    2020: (20,),
                },
            )

            def decode(data, output, media_id, _decoder, *, bank=None):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(data).hexdigest(),
                    hashlib.sha256(data).hexdigest(),
                    bank,
                )

            _path, report = build_story_voice_candidates(
                story,
                bank_index,
                ["Hero"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, _filename: snapshot,
                media_decoder=decode,
                analyzer=clean_metrics,
                include_unlinked_bank_media=True,
            )

        self.assertEqual(report["bank_inventory_scope"], "unambiguous_exact_bank")
        self.assertEqual({candidate["media_id"] for candidate in report["candidates"]}, {10, 20})

    def test_player_mode_skips_unrouted_media_from_a_shared_bank(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            hero = story_line(1, "Hero line.")
            other = story_line(2, "Other line.", media_id=40)
            other["voice_character"] = "Other"
            other["speaker"] = "Other"
            other["source_event"] = "play_other_line"
            story = write_story(root / "story.jsonl", [hero, other])
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio_root)
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"hero", 20: b"unknown", 40: b"other"},
                routes={
                    wwise_event_id("play_hero_line"): (10,),
                    wwise_event_id("play_other_line"): (40,),
                    2020: (20,),
                },
            )

            def decode(data, output, media_id, _decoder, *, bank=None):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(data).hexdigest(),
                    hashlib.sha256(data).hexdigest(),
                    bank,
                )

            _path, report = build_story_voice_candidates(
                story,
                bank_index,
                ["Hero"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, _filename: snapshot,
                media_decoder=decode,
                analyzer=clean_metrics,
                include_unlinked_bank_media=True,
            )

        self.assertEqual({candidate["media_id"] for candidate in report["candidates"]}, {10})

    def test_player_mode_accepts_multiple_portraits_of_one_character(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = story_line(1, "First expression.")
            second = story_line(2, "Second expression.", portrait="hero-smile.png")
            story = write_story(root / "story.jsonl", [first, second])
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio_root)
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"routed", 20: b"unlinked"},
                routes={
                    wwise_event_id("play_hero_line"): (10,),
                    2020: (20,),
                },
            )

            def decode(data, output, media_id, _decoder, *, bank=None):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(data).hexdigest(),
                    hashlib.sha256(data).hexdigest(),
                    bank,
                )

            _path, report = build_story_voice_candidates(
                story,
                bank_index,
                ["Hero"],
                root / "candidates",
                decoder="true",
                bank_loader=lambda _index, _filename: snapshot,
                media_decoder=decode,
                analyzer=clean_metrics,
                include_unlinked_bank_media=True,
            )

        self.assertEqual(
            {candidate["media_id"] for candidate in report["candidates"]},
            {10, 20},
        )
        self.assertEqual(len(report["candidates"]), 3)

    def test_include_all_bank_media_rejects_ambiguous_portrait_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(
                root / "story.jsonl",
                [
                    story_line(1, "Adult.", portrait="adult.png"),
                    story_line(2, "Young.", portrait="young.png"),
                ],
            )
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio_root)
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"routed media"},
                routes={wwise_event_id("play_hero_line"): (10,)},
            )

            with self.assertRaisesRegex(
                StoryVoiceCandidateError, "one exact role/portrait identity"
            ):
                build_story_voice_candidates(
                    story,
                    bank_index,
                    ["Hero"],
                    root / "candidates",
                    decoder="true",
                    bank_loader=lambda _index, _filename: snapshot,
                    include_all_bank_media=True,
                )

    def test_include_all_bank_media_rejects_unlinked_streamed_prefetch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = write_story(root / "story.jsonl", [story_line(1, "Routed exact line.")])
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, bank = write_bank_index(root / "banks.json", audio_root)
            snapshot = BankSnapshot(
                path=bank,
                sha256=hashlib.sha256(bank.read_bytes()).hexdigest(),
                media={10: b"routed media", 20: b"embedded prefetch"},
                routes={
                    wwise_event_id("play_hero_line"): (10,),
                    2020: (20,),
                },
                streamed_routes={2020: (20,)},
            )

            with self.assertRaisesRegex(StoryVoiceCandidateError, "unlinked streamed media"):
                build_story_voice_candidates(
                    story,
                    bank_index,
                    ["Hero"],
                    root / "candidates",
                    decoder="true",
                    bank_loader=lambda _index, _filename: snapshot,
                    include_all_bank_media=True,
                )

    def test_refuses_to_replace_an_existing_candidate_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "candidates"
            output.mkdir()
            story = write_story(root / "story.jsonl", [story_line(1, "Hero")])
            audio_root = root / "audio"
            audio_root.mkdir()
            bank_index, _bank = write_bank_index(root / "banks.json", audio_root)

            with self.assertRaisesRegex(StoryVoiceCandidateError, "already exists"):
                build_story_voice_candidates(
                    story,
                    bank_index,
                    ["Hero"],
                    output,
                    decoder="true",
                )


if __name__ == "__main__":
    unittest.main()
