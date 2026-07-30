"""The boundary. Nothing crosses it unvalidated, in either direction.

Both `decode` and `encode` check against the JSON Schema, not just the
models. The models cannot express every rule the schema does (patterns,
lengths, bounds, `date-time`), so a model that type-checks can still be an
illegal message. Checking the schema on the way out means the brain cannot
emit something it would refuse to accept.

Serialization uses `exclude_unset`, so a field absent on the way in stays
absent on the way out. The distinction matters: `"error": null` and no
`error` key at all are both legal, and a flight recorder that silently
rewrites one into the other is not a faithful record (ADR-0005).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from wire.models import MESSAGE_ADAPTER, Message
from wire.validation import ProtocolValidationError, validate_message


class MalformedFrameError(ValueError):
    """A frame that was not even JSON. SPEC section 6.11 code `malformed`."""


def decode(raw: str | bytes) -> Message:
    """Parse one inbound wire frame into a validated model.

    Raises `MalformedFrameError` if it is not JSON, or
    `ProtocolValidationError` if it is JSON but not a legal message.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise MalformedFrameError(f"frame is not valid JSON: {exc}") from exc

    return decode_object(payload)


def decode_object(payload: Any) -> Message:
    """Validate an already-parsed object into a model.

    Schema first, then the model. The schema produces the better diagnostic
    (it knows the message type dispatched to the wrong payload shape), and
    its verdict is the one the protocol is defined by.
    """
    validate_message(payload)

    try:
        return MESSAGE_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        # Schema-valid but model-invalid means the two disagree, which is a
        # bug in the models rather than in the message (CLAUDE.md rule 3).
        raise ProtocolValidationError(
            f"message satisfies the schema but not the models, which is a bug "
            f"in the models rather than in the message: {exc}",
            [],
        ) from exc


def to_object(message: Message) -> dict[str, Any]:
    """Render a model as the plain object that goes on the wire, validated."""
    payload = MESSAGE_ADAPTER.dump_python(message, by_alias=True, exclude_unset=True, mode="json")
    validate_message(payload)
    return payload


def encode(message: Message) -> str:
    """Render a model as one outbound wire frame, validated.

    v1 is JSON text frames, UTF-8 (SPEC section 3.3).
    """
    return json.dumps(to_object(message), separators=(",", ":"), ensure_ascii=False)
