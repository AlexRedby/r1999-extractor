import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

forbidden_extensions = {".bnk", ".wem", ".wav", ".ogg", ".mp3", ".dat"}
forbidden_names = {
    "story-index.jsonl",
    "generation-queue.jsonl",
    "english-bank-index.json",
    "dialogue-index.json",
    "npc-catalog.json",
    "npc-catalog-overlay.json",
}
maximum_binary_size = 512 * 1024


@dataclass(frozen=True)
class RepositoryViolation:
    path: str
    reason: str


def check_repository_paths(root, paths):
    root = Path(root).resolve()
    violations = []
    for relative in paths:
        relative = Path(relative)
        path = root / relative
        normalized = relative.as_posix()
        if normalized.startswith("tests/fixtures/"):
            continue
        if normalized.startswith("data/"):
            violations.append(RepositoryViolation(normalized, "game-derived data directory"))
            continue
        if relative.name in forbidden_names:
            violations.append(RepositoryViolation(normalized, "generated game artifact"))
            continue
        if relative.suffix.casefold() in forbidden_extensions:
            violations.append(RepositoryViolation(normalized, "game audio or encrypted payload"))
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > maximum_binary_size and relative.suffix.casefold() not in {
            ".py",
            ".md",
            ".toml",
            ".lock",
        }:
            violations.append(RepositoryViolation(normalized, "unexpected large binary"))
    return tuple(violations)


def tracked_paths(root):
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    root = Path(root)
    return tuple(
        decoded
        for value in result.stdout.split(b"\0")
        if value
        for decoded in (value.decode("utf-8"),)
        if (root / decoded).is_file()
    )


def create_parser():
    parser = argparse.ArgumentParser(
        description="Reject tracked extracted game content and generated artifacts."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        violations = check_repository_paths(options.root, tracked_paths(options.root))
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Unable to inspect repository: {error}", file=sys.stderr)
        return 2
    for violation in violations:
        print(f"{violation.path}: {violation.reason}", file=sys.stderr)
    if violations:
        return 1
    print("Repository contains code, documentation, and synthetic fixtures only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
