import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.story_index import load_story_index

from r1999extractor.reverse1999_index import index_version
from r1999extractor.story_index import (
    Reverse1999StoryError,
    add_story_context,
    annotate_activity220_story_lines,
    annotate_anecdote_lines,
    annotate_main_story_episode_lines,
    build_story_audio_resolver,
    build_story_collections,
    classify_speakable_english,
    extract_hero_story_plot_lines,
    parse_story_document,
    write_story_index,
)


def payload(speaker, text, *, voice="", portrait=""):
    values = [None] * 18
    values[1] = [1.0, 1.0, 2.5]
    values[11] = ["", "", speaker]
    values[13] = portrait
    values[14] = voice
    values[15] = ["", "", text]
    return values


class StoryIndexTest(unittest.TestCase):
    def test_rebuilds_bank_index_when_new_bank_is_installed(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            old_bank = root / "old.bnk"
            old_bank.write_bytes(b"old")
            old_stat = old_bank.stat()
            index_path = root / "index.json"
            old_index = {
                "version": index_version,
                "game_audio_directory": str(root),
                "banks": [
                    {
                        "path": old_bank.name,
                        "filename": old_bank.name,
                        "size": old_stat.st_size,
                        "mtime_ns": old_stat.st_mtime_ns,
                    }
                ],
            }
            index_path.write_text(json.dumps(old_index), encoding="utf-8")
            new_bank = root / "new.bnk"
            new_bank.write_bytes(b"new")
            new_index = {
                "version": index_version,
                "game_audio_directory": str(root),
                "banks": [],
            }

            with (
                patch(
                    "r1999extractor.story_index.load_config_directory",
                    return_value=({}, {}),
                ),
                patch(
                    "r1999extractor.story_index.build_bank_index",
                    return_value=(new_index, index_path),
                ) as rebuild,
            ):
                build_story_audio_resolver(
                    config_directory=root,
                    bank_index_path=index_path,
                    game_audio_directory=root,
                )

        rebuild.assert_called_once_with(root, output=index_path.resolve(), progress=None)

    def test_parses_dialogue_and_narration_into_generic_lines(self):
        document = [
            "title",
            "",
            [
                [7, "step", payload("Brimley", "Hello.", voice="play_7", portrait="7.png")],
                [8, "step", payload("", "Rain falls.")],
            ],
        ]

        lines = parse_story_document(document, "json_story_step_24006")

        self.assertEqual(lines[0].line_id, "reverse1999:24006:7")
        self.assertEqual(lines[0].speaker, "Brimley")
        self.assertEqual(lines[0].source_voice_id, "play_7")
        self.assertEqual(lines[0].source_voice_spec, "play_7")
        self.assertEqual(lines[0].voice_character, "Brimley")
        self.assertEqual(lines[1].speaker, "Narrator")
        self.assertEqual(lines[1].kind, "narration")

    def test_writes_versioned_jsonl_contract(self):
        lines = parse_story_document(
            ["title", "", [[1, "step", payload("A", "Text")]]],
            "json_story_step_1001",
        )
        lines = [
            replace(
                lines[0],
                audio_status="installed",
                source_voice_id="7",
            )
        ]
        with TemporaryDirectory() as temporary_directory:
            output = write_story_index(lines, Path(temporary_directory) / "story.jsonl")
            records = [json.loads(row) for row in output.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["schema"], "vntts.story-index")
        self.assertEqual(records[0]["schema_version"], 1)
        self.assertEqual(records[1]["record_type"], "line")
        self.assertEqual(len(records[1]["text_sha256"]), 64)
        self.assertEqual(records[1]["source_audio_status"], "available")
        self.assertEqual(records[1]["source_audio_id"], "7")
        self.assertNotIn("collections", records[0])

    def test_writes_game_derived_collection_catalog_and_line_membership(self):
        main = annotate_main_story_episode_lines(
            parse_story_document(
                ["title", "", [[1, "step", payload("A", "Main line.")]]],
                "json_story_step_101301",
            ),
            {"episode": "A Long Road"},
            {"json_episode": [[1, 13, 1, "episode", "", "", "", 101301, "", 0]]},
        )[0]
        anecdote = annotate_anecdote_lines(
            parse_story_document(
                ["title", "", [[1, "step", payload("B", "Anecdote line.")]]],
                "json_story_step_301801",
            ),
            {"story": "The Eaglet Takes Wing", "episode": "Departure"},
            {
                "json_hero_story": [[1, 1901, "", "", "", 0, 0, 1, "story", ""]],
                "json_episode": [[1, 1901, 1, "episode", "", "", "", 301801]],
            },
        )[0]

        with TemporaryDirectory() as temporary_directory:
            output = write_story_index([anecdote, main], Path(temporary_directory) / "story.jsonl")
            records = [json.loads(row) for row in output.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(
            records[0]["collections"],
            [
                {
                    "collection_id": "reverse1999:main-story:13",
                    "kind": "main_story",
                    "order": 13,
                    "title": "Chapter 13",
                },
                {
                    "collection_id": "reverse1999:anecdote:1901",
                    "kind": "anecdote",
                    "order": 1,
                    "title": "The Eaglet Takes Wing",
                },
            ],
        )
        self.assertEqual(records[1]["collection_id"], "reverse1999:anecdote:1901")
        self.assertEqual(records[2]["collection_id"], "reverse1999:main-story:13")

    def test_collection_extensions_remain_readable_by_schema_v1_loader(self):
        line = replace(
            parse_story_document(
                ["title", "", [[1, "step", payload("A", "A story line.")]]],
                "json_story_step_1001",
            )[0],
            collection_id="reverse1999:anecdote:1",
            collection_title="A Story",
            collection_kind="anecdote",
            collection_order=1,
        )
        with TemporaryDirectory() as temporary_directory:
            output = write_story_index([line], Path(temporary_directory) / "story.jsonl")
            metadata, loaded_lines = load_story_index(output)

        self.assertEqual(metadata["collections"][0]["collection_id"], line.collection_id)
        self.assertEqual(loaded_lines[0].line_id, line.line_id)

    def test_filters_non_english_and_test_placeholder_lines_by_default(self):
        document = [
            "title",
            "",
            [
                [1, "step", payload("", "只有中文")],
                [2, "step", payload("", "A proper English line.")],
            ],
        ]
        lines = parse_story_document(document, "json_story_step_24006")
        all_lines = parse_story_document(
            document, "json_story_step_24006", include_non_speakable=True
        )
        self.assertEqual([line.text for line in lines], ["A proper English line."])
        self.assertEqual(len(all_lines), 2)
        self.assertFalse(all_lines[0].speakable)
        self.assertEqual(all_lines[0].filter_reason, "no_latin_text")
        self.assertEqual(classify_speakable_english("over", "128"), (False, "test_asset"))

    def test_strips_rich_text_and_adds_neighbor_context(self):
        lines = parse_story_document(
            [
                "title",
                "",
                [
                    [1, "step", payload("A", "<b>First</b> line.")],
                    [2, "step", payload("B", "Second line.")],
                ],
            ],
            "json_story_step_24006",
        )
        lines = add_story_context(lines)
        self.assertEqual(lines[0].text, "First line.")
        self.assertEqual(lines[0].next_text, "Second line.")
        self.assertEqual(lines[1].previous_text, "First line.")

    def test_classifies_anecdote_assets_without_duplicating_lines(self):
        lines = parse_story_document(
            ["title", "", [[1, "step", payload("A", "Anecdote line.")]]],
            "json_story_step_301801",
        )
        language = {
            "hero": "Test Character",
            "story": "Synthetic Anecdote",
            "episode": "Synthetic Episode",
        }
        tables = {
            "json_hero_story": [[1, 1901, "", "", "hero", 0, 0, 1, "story", "fallback"]],
            "json_episode": [[190101, 1901, 4, "episode", "fallback", "", "", 301801]],
        }

        annotated = annotate_anecdote_lines(lines, language, tables)

        self.assertEqual(len(annotated), 1)
        self.assertEqual(annotated[0].source_kind, "anecdote")
        self.assertEqual(annotated[0].story_group, "1901")
        self.assertEqual(annotated[0].story_title, "Synthetic Anecdote")
        self.assertEqual(annotated[0].episode_title, "Synthetic Episode")
        self.assertEqual(annotated[0].collection_id, "reverse1999:anecdote:1901")
        self.assertEqual(annotated[0].collection_kind, "anecdote")

    def test_classifies_activity220_character_story_assets(self):
        lines = parse_story_document(
            ["title", "", [[1, "step", payload("Rhiannon", "Character story line.")]]],
            "json_story_step_314601",
        )
        language = {
            "activity": "The You That's Meant To Be",
            "episode": "The Young Traveler",
        }
        tables = {
            "json_activity": [[13710, "activity"]],
            "json_activity220_episode": [[13710, 1371001, 0, 0, "episode", 314601]],
        }

        annotated = annotate_activity220_story_lines(lines, language, tables)

        self.assertEqual(len(annotated), 1)
        self.assertEqual(annotated[0].source_kind, "activity_story")
        self.assertEqual(annotated[0].story_group, "13710")
        self.assertEqual(annotated[0].story_title, "The You That's Meant To Be")
        self.assertEqual(annotated[0].episode_title, "The Young Traveler")
        self.assertEqual(
            annotated[0].collection_id,
            "reverse1999:character-story:activity220:13710",
        )
        self.assertEqual(annotated[0].collection_kind, "character_story")

    def test_maps_split_main_story_assets_to_player_visible_episodes(self):
        lines = [
            *parse_story_document(
                ["title", "", [[1, "step", payload("A", "First part.")]]],
                "json_story_step_101201",
            ),
            *parse_story_document(
                ["title", "", [[1, "step", payload("A", "Second part.")]]],
                "json_story_step_101202",
            ),
            *parse_story_document(
                ["title", "", [[1, "step", payload("B", "Next episode.")]]],
                "json_story_step_101203",
            ),
        ]
        language = {"named": "The Eternal Autumn"}
        tables = {
            "json_episode": [
                [1201, 12, 1, "", "", "", "", 101201, "", 101202],
                [11202, 112, 4, "named", "named", "", "", 101203, "", 0],
            ]
        }

        annotated = annotate_main_story_episode_lines(lines, language, tables)

        self.assertEqual(annotated[0].episode_title, "Episode 1")
        self.assertEqual(annotated[1].episode_title, "Episode 1")
        self.assertEqual(annotated[2].episode_title, "The Eternal Autumn")
        self.assertEqual({line.story_group for line in annotated}, {"main:12"})
        self.assertEqual({line.collection_id for line in annotated}, {"reverse1999:main-story:12"})

    def test_extracts_config_only_hero_story_dialogue_and_narration(self):
        language = {
            "hero": "Test Hero",
            "story": "Synthetic Story",
            "episode": "Synthetic Chapter",
            "role": "{roleName}",
            "dialogue": "A synthetic dialogue line.",
            "aside": "A synthetic narration line.",
            "control": "Tap to continue",
        }
        tables = {
            "json_hero_story": [[26, 0, "", "", "hero", 0, 0, 26, "story", "fallback"]],
            "json_hero_story_plot_group": [[303701, 26, "episode", "fallback", 0, "", 1, "hero"]],
            "json_hero_story_plot": [
                [303701004, 303701, "dialog", "", "role", "dialogue"],
                [303701005, 303701, "aside", "", "", "aside"],
                [303701006, 303701, "control", "", "", "control"],
            ],
        }

        lines = extract_hero_story_plot_lines(language, tables)

        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].speaker, "Test Hero")
        self.assertEqual(lines[0].source_kind, "hero_story_plot")
        self.assertEqual(lines[0].audio_status, "no_audio")
        self.assertEqual(lines[0].story_title, "Synthetic Story")
        self.assertEqual(lines[0].episode_title, "Synthetic Chapter")
        self.assertEqual(lines[0].collection_id, "reverse1999:anecdote:hero-story:26")
        self.assertEqual(lines[0].collection_order, 1)
        self.assertEqual(lines[0].next_text, "A synthetic narration line.")
        self.assertEqual(lines[1].kind, "narration")

    def test_applies_verified_voice_reveal_without_rewriting_display_speaker(self):
        language = {
            "hero": "Silverwing Eagle",
            "story": "The Eaglet Takes Wing",
            "episode": "An Eaglet on the Trail",
            "unknown": "???",
            "scope": "A familiar scope glint flashes.",
            "revealed": '"Thank you all for your cooperation."',
            "recognition": "Hearing that voice, Eagle lets out a sigh of relief.",
            "butterfly": "Lorentz Butterfly",
            "confirmed": "This tedious chase ends now.",
        }
        tables = {
            "json_hero_story": [[31, 0, "", "", "hero", 0, 0, 31, "story", "fallback"]],
            "json_hero_story_plot_group": [[315407, 31, "episode", "fallback", 0, "", 1, "hero"]],
            "json_hero_story_plot": [
                [315407062, 315407, "aside", "", "", "scope", 0, "3", "", "37500175#1", ""],
                [315407063, 315407, "dialog", "", "unknown", "revealed", 0, "", "", "", ""],
                [315407064, 315407, "aside", "", "", "recognition", 0, "", "", "", ""],
                [
                    315407065,
                    315407,
                    "dialog",
                    "",
                    "butterfly",
                    "confirmed",
                    0,
                    "",
                    "",
                    "",
                    "",
                ],
            ],
        }

        lines = extract_hero_story_plot_lines(language, tables)

        self.assertEqual(
            [line.line_id for line in lines],
            [
                "reverse1999:hero-story-plot:315407062",
                "reverse1999:hero-story-plot:315407063",
                "reverse1999:hero-story-plot:315407064",
                "reverse1999:hero-story-plot:315407065",
            ],
        )
        self.assertEqual(lines[1].speaker, "???")
        self.assertEqual(lines[1].voice_character, "Marguerite")
        self.assertEqual(lines[3].speaker, "Lorentz Butterfly")
        self.assertEqual(lines[3].voice_character, "Marguerite")

    def test_rejects_conflicting_collection_metadata(self):
        lines = parse_story_document(
            [
                "title",
                "",
                [
                    [1, "step", payload("A", "One.")],
                    [2, "step", payload("A", "Two.")],
                ],
            ],
            "json_story_step_1001",
        )
        first = replace(
            lines[0],
            collection_id="reverse1999:test:1",
            collection_title="One",
            collection_kind="anecdote",
            collection_order=1,
        )
        second = replace(
            lines[1],
            collection_id="reverse1999:test:1",
            collection_title="Two",
            collection_kind="anecdote",
            collection_order=1,
        )

        with self.assertRaisesRegex(
            Reverse1999StoryError,
            "Conflicting collection metadata",
        ):
            build_story_collections([first, second])


if __name__ == "__main__":
    unittest.main()
