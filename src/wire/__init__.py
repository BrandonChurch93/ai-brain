"""The wire: envelope models, schema validation, and transport.

`protocol/schemas/protocol.schema.json` is the source of truth for the
format; this package loads it and validates against it rather than
restating it (CLAUDE.md rule 3).
"""

from wire.envelope import Envelope, Timestamp
from wire.schema import (
    SUBPROTOCOL,
    SchemaNotFoundError,
    capability_classes,
    message_types,
    protocol_schema,
    schema_id,
    schema_path,
)
from wire.validation import (
    ProtocolValidationError,
    is_valid,
    iter_errors,
    validate_message,
    validator,
)

__all__ = [
    "SUBPROTOCOL",
    "Envelope",
    "ProtocolValidationError",
    "SchemaNotFoundError",
    "Timestamp",
    "capability_classes",
    "is_valid",
    "iter_errors",
    "message_types",
    "protocol_schema",
    "schema_id",
    "schema_path",
    "validate_message",
    "validator",
]
