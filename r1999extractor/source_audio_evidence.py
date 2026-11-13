"""Retain exact checksum-bound semantic sidecars in story successors."""

import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.atomic_io import atomic_write_bytes

from r1999extractor.story_audio import StoryAudioResolutionError

SEMANTIC_EVIDENCE_FILENAME = "source-audio-semantic-evidence.json"


def copy_bound_source_audio_semantic_evidence(metadata, source_story, destination_story):
    """Copy retained authority unchanged, refusing to replace other story evidence."""
    binding = metadata.get("source_audio_semantics")
    if binding is None:
        return
    if not isinstance(binding, dict):
        raise StoryAudioResolutionError("Retained source-audio evidence binding is invalid")
    source = Path(source_story).parent / SEMANTIC_EVIDENCE_FILENAME
    destination = Path(destination_story).parent / SEMANTIC_EVIDENCE_FILENAME
    try:
        payload = source.read_bytes()
        evidence = json.loads(payload)
    except (OSError, ValueError) as error:
        raise StoryAudioResolutionError(
            f"Unable to retain source-audio evidence: {error}"
        ) from error
    if (
        hashlib.sha256(payload).hexdigest() != binding.get("evidence_sha256")
        or not isinstance(binding.get("evidence_id"), str)
        or not isinstance(evidence, dict)
        or evidence.get("evidence_id") != binding.get("evidence_id")
    ):
        raise StoryAudioResolutionError("Retained source-audio evidence binding changed")
    if destination.is_symlink():
        raise StoryAudioResolutionError("Successor source-audio evidence must not be a symlink")
    if destination.exists():
        if destination.read_bytes() != payload:
            raise StoryAudioResolutionError(
                "Successor directory contains different source-audio evidence"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".retained-evidence-", dir=destination.parent) as directory:
        staged = Path(directory) / destination.name
        atomic_write_bytes(staged, payload)
        try:
            os.link(staged, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != payload:
                raise StoryAudioResolutionError(
                    "Successor directory contains different source-audio evidence"
                ) from None
