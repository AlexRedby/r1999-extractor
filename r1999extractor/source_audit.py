import argparse
import json
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json

from r1999extractor.cli import cli_error
from r1999extractor.reverse1999_config import find_game_config_directory, load_config_directory
from r1999extractor.settings import get_local_data_directory
from r1999extractor.structured_story import audit_story_like_tables

default_output = get_local_data_directory() / "reverse1999" / "story-source-audit.json"


def create_parser():
    parser = argparse.ArgumentParser(description="Audit localized story-like config tables.")
    parser.add_argument("--config-directory", type=Path)
    parser.add_argument("--output", type=Path, default=default_output)
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    config_directory = options.config_directory or find_game_config_directory()
    if config_directory is None:
        return cli_error("Unable to find installed game configs; pass --config-directory")
    try:
        language, tables = load_config_directory(config_directory)
        report = audit_story_like_tables(language, tables)
        report.update(
            {
                "schema": "r1999.story-source-audit",
                "schema_version": 1,
                "config_directory": str(Path(config_directory).resolve()),
            }
        )
        atomic_write_json(options.output, report, sort_keys=True)
    except (OSError, json.JSONDecodeError) as error:
        return cli_error(error)
    print(
        f"Reviewed {report['reviewed_table_count']} story-like tables; "
        f"{report['handled_table_count']} have explicit extraction schemas"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
