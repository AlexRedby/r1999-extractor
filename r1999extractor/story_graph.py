"""Decode the game's story-group edges without treating step IDs as positions."""

from __future__ import annotations


class StoryGraphError(ValueError):
    pass


def parse_story_graph(document, step_ids, *, source):
    """Keep serialized branch order, including references to absent raw steps."""
    if not isinstance(document, list):
        raise StoryGraphError(f"Story group asset {source} has an unsupported structure")
    successors = dict.fromkeys(step_ids, ())
    seen_sources = set()
    for position, group in enumerate(document, start=1):
        if not isinstance(group, list):
            raise StoryGraphError(f"Story group asset {source} group {position} is not a list")
        if not group:
            continue
        targets = []
        source_id = None
        for edge in group:
            if not isinstance(edge, list) or len(edge) != 3:
                raise StoryGraphError(
                    f"Story group asset {source} group {position} has a malformed edge"
                )
            start, target, label = edge
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (start, target)
            ) or not isinstance(label, str):
                raise StoryGraphError(
                    f"Story group asset {source} group {position} has an invalid edge"
                )
            if source_id is not None and start != source_id:
                raise StoryGraphError(
                    f"Story group asset {source} group {position} mixes source IDs"
                )
            if target in targets:
                raise StoryGraphError(f"Story group asset {source} repeats edge {start}:{target}")
            source_id = start
            targets.append(target)
        if source_id in seen_sources:
            raise StoryGraphError(f"Story group asset {source} repeats source {source_id}")
        seen_sources.add(source_id)
        successors[source_id] = tuple(targets)
    return successors


def ordered_step_ids(successors):
    """Enumerate roots and branches stably; this does not choose a player route."""
    incoming = {target for targets in successors.values() for target in targets}
    roots = [step_id for step_id in successors if step_id not in incoming]
    visited = set()
    ordered = []
    for start in [*roots, *successors]:
        pending = [start]
        while pending:
            step_id = pending.pop()
            if step_id in visited:
                continue
            visited.add(step_id)
            ordered.append(step_id)
            pending.extend(reversed(successors.get(step_id, ())))
    return ordered
