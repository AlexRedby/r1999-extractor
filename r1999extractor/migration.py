import argparse
import shutil
from pathlib import Path

from platformdirs import user_data_path

from r1999extractor.cli import cli_error
from r1999extractor.settings import get_local_data_directory


class MigrationError(RuntimeError):
    pass


def legacy_vntts_data_directory():
    return user_data_path("VisualNovelTextToSpeech", appauthor=False)


def migration_items(source=None, destination=None):
    source = Path(source or legacy_vntts_data_directory()).expanduser().resolve()
    destination = Path(destination or get_local_data_directory()).expanduser().resolve()
    return (
        (source / "reverse1999", destination / "reverse1999"),
        (
            source / "voice-packs" / "reverse1999",
            destination / "voice-packs" / "reverse1999",
        ),
    )


def migrate_legacy_data(source=None, destination=None, *, dry_run=False):
    results = []
    for old, new in migration_items(source, destination):
        if not old.exists():
            results.append(("missing", old, new))
            continue
        if new.exists():
            results.append(("exists", old, new))
            continue
        if not dry_run:
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(old, new)
            except OSError as error:
                raise MigrationError(f"Unable to copy {old} to {new}: {error}") from error
        results.append(("would-copy" if dry_run else "copied", old, new))
    return tuple(results)


def create_parser():
    parser = argparse.ArgumentParser(
        description="Copy existing Reverse: 1999 indexes, reviews, and voice packs from VNTTS without deleting the originals."
    )
    parser.add_argument("--source", type=Path, help="Legacy VNTTS data directory")
    parser.add_argument("--destination", type=Path, help="Extractor data directory")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        results = migrate_legacy_data(options.source, options.destination, dry_run=options.dry_run)
    except MigrationError as error:
        return cli_error(error)
    for status, old, new in results:
        print(f"{status}: {old} -> {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
