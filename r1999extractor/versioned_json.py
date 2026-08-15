"""Versioned JSON persistence for extractor-owned state and reports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json


class VersionedJSONError(RuntimeError):
    pass


@dataclass(frozen=True)
class VersionedJSONCodec:
    schema: str
    version: int
    description: str = "versioned JSON document"

    def new(self, **fields):
        return {
            "schema": self.schema,
            "schema_version": self.version,
            **fields,
        }

    def validate(self, document):
        if not isinstance(document, dict):
            raise VersionedJSONError(f"{self.description} must be a JSON object")
        if document.get("schema") != self.schema or document.get("schema_version") != self.version:
            raise VersionedJSONError(f"Unsupported {self.description} schema")
        return document

    def load(self, path):
        path = Path(path).expanduser().resolve()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise VersionedJSONError(
                f"Unable to read {self.description} {path}: {error}"
            ) from error
        return self.validate(document)

    def write(self, path, document, *, sort_keys=False):
        self.validate(document)
        return atomic_write_json(path, document, sort_keys=sort_keys)
