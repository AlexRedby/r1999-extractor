"""Optional progress side channel shared by source-audio authoring commands."""

from pathlib import Path
from time import monotonic
from typing import Callable, Literal, TypedDict

from vntts_artifacts.atomic_io import atomic_write_json

SourceAudioStage = Literal["duration", "semantics", "references", "portraits"]
SourceAudioState = Literal["analyzing", "publishing"]


class SourceAudioProgress(TypedDict):
    schema: Literal["r1999.source-audio-progress"]
    schema_version: Literal[1]
    stage: SourceAudioStage
    processed: int
    total: int
    state: SourceAudioState


SourceAudioProgressCallback = Callable[[SourceAudioProgress], None]


def report_source_audio_progress(
    callback: SourceAudioProgressCallback | None,
    stage: SourceAudioStage,
    processed: int,
    total: int,
    state: SourceAudioState = "analyzing",
) -> None:
    if callback is not None:
        callback({
            "schema": "r1999.source-audio-progress",
            "schema_version": 1,
            "stage": stage,
            "processed": processed,
            "total": total,
            "state": state,
        })


class SourceAudioProgressFile:
    """Keep intermediate snapshots small; always publish boundaries and state changes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._last_write = 0.0
        self._last_state = None

    def __call__(self, progress: SourceAudioProgress) -> None:
        now = monotonic()
        state = (progress["stage"], progress["state"])
        if (
            progress["processed"] not in {0, progress["total"]}
            and state == self._last_state
            and now - self._last_write < 0.2
        ):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, progress)
        self._last_write = now
        self._last_state = state
