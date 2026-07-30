"""Locate and load the protocol schema.

`protocol/schemas/protocol.schema.json` is the source of truth (CLAUDE.md
rule 3). Nothing in this module restates what the schema says; the constants
below are read out of it so they cannot drift.
"""

from __future__ import annotations

import json
import os
import re
from functools import cache
from pathlib import Path
from typing import Any

_VERSION_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

SCHEMA_RELATIVE_PATH = Path("protocol/schemas/protocol.schema.json")

#: Override the schema location. Useful when the package is installed away
#: from the repo that carries `protocol/`.
SCHEMA_PATH_ENV_VAR = "BRAIN_PROTOCOL_SCHEMA"

#: WebSocket subprotocol naming the compatibility family (SPEC section 3.2).
#: Generic by ADR-0000 rule 4: never the project name.
SUBPROTOCOL = "body-adapter-protocol.v1"


class SchemaNotFoundError(RuntimeError):
    """The protocol schema could not be located on disk."""


def schema_path() -> Path:
    """Resolve the schema file, preferring an explicit env override.

    Without the override, walk up from this file looking for the repo layout.
    """
    override = os.environ.get(SCHEMA_PATH_ENV_VAR)
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise SchemaNotFoundError(
                f"{SCHEMA_PATH_ENV_VAR} points at {candidate}, which is not a file"
            )
        return candidate

    for parent in Path(__file__).resolve().parents:
        candidate = parent / SCHEMA_RELATIVE_PATH
        if candidate.is_file():
            return candidate

    raise SchemaNotFoundError(
        f"could not find {SCHEMA_RELATIVE_PATH} above {__file__}; "
        f"set {SCHEMA_PATH_ENV_VAR} to point at it"
    )


@cache
def protocol_schema() -> dict[str, Any]:
    """The parsed schema. Cached: the file does not change while running."""
    return json.loads(schema_path().read_text(encoding="utf-8"))


def _definition(name: str) -> dict[str, Any]:
    return protocol_schema()["$defs"][name]


@cache
def message_types() -> frozenset[str]:
    """Every legal envelope `type`, read from the schema."""
    return frozenset(_definition("Envelope")["properties"]["type"]["enum"])


@cache
def capability_classes() -> frozenset[str]:
    """The v1 capability class registry, read from the schema."""
    return frozenset(_definition("Capability")["properties"]["class"]["enum"])


@cache
def schema_id() -> str:
    """The schema's `$id`, which carries the version it was published under."""
    return protocol_schema()["$id"]


@cache
def protocol_version() -> str:
    """The version string this schema publishes, e.g. `2026-07-29`.

    Read off the `$id` rather than written down again, so the schema and the
    version the brain negotiates with cannot disagree (SPEC section 5).
    """
    version = schema_id().rsplit(":", 1)[-1]
    if not _VERSION_PATTERN.fullmatch(version):
        raise SchemaNotFoundError(
            f"schema $id {schema_id()!r} does not end in a YYYY-MM-DD version string"
        )
    return version


@cache
def supported_versions() -> tuple[str, ...]:
    """Every version the brain accepts, newest first (SPEC section 9.3).

    One entry today. A future breaking change adds the old version here and
    keeps it until every body has migrated.
    """
    return (protocol_version(),)
