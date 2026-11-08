"""Optional progress side channel shared by source-audio authoring commands."""

import json
import sys
from time import monotonic
from typing import Callable, Literal, TextIO, TypedDict

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
        callback(
            {
                "schema": "r1999.source-audio-progress",
                "schema_version": 1,
                "stage": stage,
                "processed": processed,
                "total": total,
                "state": state,
            }
        )


class SourceAudioProgressStream:
    """Throttle intermediate events; always emit boundaries and state changes."""

    def __init__(self, stream: TextIO | None = None):
        self.stream = sys.stdout if stream is None else stream
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
        self.stream.write("\nR1999_PROGRESS " + json.dumps(progress, separators=(",", ":")) + "\n")
        self.stream.flush()
        self._last_write = now
        self._last_state = state
