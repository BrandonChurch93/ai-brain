"""Schema validation at the wire boundary.

Every inbound and outbound message goes through here. The rule that matters
for forward compatibility (SPEC section 4): the schema deliberately does not
set `additionalProperties: false`, so unknown fields validate fine and are
carried through untouched. Unknown message *types* are a different matter and
do fail, because the type enum is closed.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from wire.schema import protocol_schema


class ProtocolValidationError(ValueError):
    """A message did not satisfy the protocol schema."""

    def __init__(self, message: str, errors: list[ValidationError]) -> None:
        super().__init__(message)
        self.errors = errors


@cache
def validator() -> Draft202012Validator:
    """The shared validator. Format checking is on, so `date-time` is enforced."""
    return Draft202012Validator(protocol_schema(), format_checker=FormatChecker())


def iter_errors(message: Any) -> list[ValidationError]:
    """Every schema violation in `message`. Empty means valid.

    Errors are flattened to leaves. The envelope dispatches on `type` through
    a chain of if/then subschemas, so a real problem like a missing `ttl_ms`
    surfaces from jsonschema wrapped in an `allOf` error whose message says
    nothing useful. The leaf is the part worth logging (ADR-0005).
    """
    leaves = [leaf for error in validator().iter_errors(message) for leaf in _leaves(error)]
    return sorted(leaves, key=lambda error: tuple(str(part) for part in error.absolute_path))


def is_valid(message: Any) -> bool:
    """True when `message` satisfies the schema."""
    return not iter_errors(message)


def validate_message(message: Any) -> None:
    """Raise `ProtocolValidationError` unless `message` satisfies the schema."""
    errors = iter_errors(message)
    if not errors:
        return

    detail = "; ".join(f"{_path(error)}: {error.message}" for error in errors)
    kind = message.get("type", "<no type>") if isinstance(message, dict) else "<not an object>"
    raise ProtocolValidationError(f"invalid {kind} message: {detail}", errors)


def _leaves(error: ValidationError) -> Iterator[ValidationError]:
    if error.context:
        for sub_error in error.context:
            yield from _leaves(sub_error)
    else:
        yield error


def _path(error: ValidationError) -> str:
    return "/".join(str(part) for part in error.absolute_path) or "<root>"
