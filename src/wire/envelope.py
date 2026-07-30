"""The envelope, as Python sees it.

Structural typing only, for readable call sites and editor help. These are
`TypedDict`s, not runtime models: validation is the schema's job (see
`wire.validation`), and pydantic models mirroring the schema arrive with the
session core. Nothing here restates a constraint the schema already states.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class Timestamp(TypedDict):
    """t_captured, stamped by the sender. Receivers log their own t_received."""

    mono_ns: int
    utc: str


class Envelope(TypedDict):
    """SPEC section 4. `session` and `span_id` are conditionally required;
    which messages require them is expressed in the schema, not here."""

    type: str
    id: str
    seq: int
    ts: Timestamp
    payload: dict[str, Any]
    session: NotRequired[str]
    trace_id: NotRequired[str]
    span_id: NotRequired[str]
