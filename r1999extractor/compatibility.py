import sys
from pathlib import Path

legacy_workflow_descriptions = {
    "r1999-generate": "bulk generation and review",
    "r1999-benchmark": "voice-model benchmarking",
    "r1999-pregenerate": "MOSS pregeneration jobs",
}


def legacy_workflow_notice(command, artifact_paths=()):
    """Describe legacy ownership and discover artifacts without modifying them."""
    description = legacy_workflow_descriptions[command]
    paths = tuple(_resolved_paths(artifact_paths))
    existing = tuple(path for path in paths if path.exists())
    if existing:
        discovery = "Discovered existing artifacts: " + ", ".join(map(str, existing))
    elif paths:
        discovery = "Legacy artifact locations: " + ", ".join(map(str, paths))
    else:
        discovery = "No legacy artifact path was supplied"
    print(
        f"Compatibility notice: {command} still runs extractor-owned {description}. "
        "It does not migrate, delete, or regenerate existing work automatically. "
        "VNTTS authoring should take ownership after its legacy-job importer is available. "
        f"{discovery}",
        file=sys.stderr,
    )


def _resolved_paths(values):
    seen = set()
    for value in values:
        if value is None:
            continue
        candidates = value if isinstance(value, (list, tuple)) else (value,)
        for candidate in candidates:
            path = Path(candidate).expanduser().resolve()
            if path not in seen:
                seen.add(path)
                yield path
