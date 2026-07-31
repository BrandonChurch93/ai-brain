"""Pydantic models mirroring the protocol schema (SPEC section 4).

Division of labour with the schema, which stays the source of truth
(CLAUDE.md rule 3):

- These models own *structure*: which fields exist, which are optional, and
  the `type` discriminant that says which payload belongs to which envelope.
  That is what Python needs to give call sites real types.
- The schema keeps *constraints*: patterns, lengths, numeric bounds, and
  formats. Restating those here would be two sources of truth for one rule,
  and the boundary in `wire.codec` schema-validates in both directions
  anyway, so nothing reaches or leaves the wire unchecked.

The literal sets below are the one unavoidable overlap, because a
discriminated union needs static literals. `tests/test_models_match_schema.py`
asserts every one of them against the schema's enums, so they cannot drift.

Unknown fields are kept, not dropped. SPEC section 4 says receivers ignore
unknown fields, meaning they must not fail on them; it does not say discard.
Discarding would quietly rewrite messages passing through the brain and cost
the flight recorder its fidelity (ADR-0005), so `extra="allow"`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# Literal sets, asserted against the schema's enums by the test suite.
CapabilityClass = Literal[
    "system",
    "camera",
    "microphone",
    "speaker",
    "display",
    "differential_drive",
    "range_sensor",
    "virtual",
]
HardwareClass = Literal["workstation", "sbc", "microcontroller", "mobile_base", "virtual"]
BootState = Literal["ok", "safe_hold"]
RejectCode = Literal["unsupported_version", "auth_failed", "malformed_hello"]
BodyState = Literal["ok", "safe_hold", "estopped", "active", "degraded"]
CommandStatus = Literal["accepted", "running", "succeeded", "failed", "expired", "rejected"]
ErrorCode = Literal[
    "malformed",
    "unknown_type",
    "unknown_capability",
    "unknown_action",
    "invalid_params",
    "busy",
    "latched_safe_state",
    "internal",
]

#: Terminal statuses end a span; exactly one per span (SPEC section 6.7).
TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "expired", "rejected"})

#: States that latch and never clear themselves (SPEC section 8.2).
LATCHED_STATES: frozenset[str] = frozenset({"safe_hold", "estopped"})


class WireModel(BaseModel):
    """Base for everything on the wire. Keeps unknown fields."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


def without_none(**fields: Any) -> dict[str, Any]:
    """Drop fields whose value is None, for building messages.

    Serialization keeps whatever was explicitly set, which is what preserves
    the difference between an absent field and a null one. The cost is that
    passing `trace_id=None` to a constructor marks it set, and the message
    then carries `"trace_id": null` where the schema wants a string or
    nothing at all. Optional envelope fields are almost always "omit if I do
    not have one", so builders route through here rather than each
    remembering the trap.

    Payload fields that are genuinely nullable, such as a command result's
    `error`, are set directly on the payload instead.
    """
    return {name: value for name, value in fields.items() if value is not None}


# Shared structures


class Timestamp(WireModel):
    """t_captured, stamped by the sender (SPEC section 4)."""

    mono_ns: int
    utc: str


class NamedVersion(WireModel):
    name: str
    version: str


class HeartbeatConfig(WireModel):
    interval_ms: int
    lease_ms: int


class CapabilityAck(WireModel):
    recognized: list[str] | None = None
    unrecognized: list[str] | None = None


class ErrorDetail(WireModel):
    code: str
    message: str


class Capability(WireModel):
    """One declared function of a body (SPEC section 7.1)."""

    id: str
    # `class` is a Python keyword, so the field is renamed and aliased back.
    capability_class: CapabilityClass = Field(alias="class")
    attributes: dict[str, Any] | None = None
    actions: list[str] | None = None
    events: list[str] | None = None
    do_command: bool = True


class Manifest(WireModel):
    """The planner's affordance set (SPEC section 7.1, ADR-0003)."""

    body_id: str
    display_name: str | None = None
    hardware_class: HardwareClass
    boot_state: BootState
    adapter: NamedVersion
    capabilities: list[Capability]


# Payloads


class HelloPayload(WireModel):
    protocol_versions: list[str]
    auth_token: str
    manifest: Manifest


class WelcomePayload(WireModel):
    protocol_version: str
    session: str
    server: NamedVersion
    utc_now: str
    heartbeat: HeartbeatConfig
    capability_ack: CapabilityAck | None = None


class RejectPayload(WireModel):
    code: RejectCode
    message: str
    supported: list[str] | None = None


class HeartbeatPayload(WireModel):
    state: BodyState
    uptime_ms: int | None = None


class EventPayload(WireModel):
    capability: str
    event: str
    data: dict[str, Any]
    droppable: bool = False


class CommandPayload(WireModel):
    capability: str
    action: str
    params: dict[str, Any]
    ttl_ms: int
    expects_result: bool = True


class CommandResultPayload(WireModel):
    status: CommandStatus
    progress: float | None = None
    data: dict[str, Any] | None = None
    error: ErrorDetail | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class EmptyPayload(WireModel):
    pass


class EstopPayload(WireModel):
    reason: str


class EstopClearPayload(WireModel):
    reason: str
    operator: str


class ErrorPayload(WireModel):
    code: ErrorCode
    message: str
    ref_id: str | None = None


# Envelopes. The schema expresses "which fields are required for which type"
# with if/then subschemas; here it is expressed as the class hierarchy.


class BaseEnvelope(WireModel):
    id: str
    seq: int
    ts: Timestamp
    trace_id: str | None = None
    span_id: str | None = None


class SessionEnvelope(BaseEnvelope):
    """Everything after the handshake carries a session (SPEC section 4)."""

    session: str


class HelloEnvelope(BaseEnvelope):
    type: Literal["hello"]
    session: str | None = None
    payload: HelloPayload


class WelcomeEnvelope(SessionEnvelope):
    type: Literal["welcome"]
    payload: WelcomePayload


class RejectEnvelope(BaseEnvelope):
    """No session: the handshake failed, so there is none to name."""

    type: Literal["reject"]
    session: str | None = None
    payload: RejectPayload


class HeartbeatEnvelope(SessionEnvelope):
    type: Literal["heartbeat"]
    payload: HeartbeatPayload


class EventEnvelope(SessionEnvelope):
    type: Literal["event"]
    payload: EventPayload


class CommandEnvelope(SessionEnvelope):
    """span_id is required: without it a body cannot deduplicate a
    retransmission, so a command could execute twice (SPEC section 6.6)."""

    type: Literal["command"]
    span_id: str
    payload: CommandPayload


class CommandResultEnvelope(SessionEnvelope):
    type: Literal["command_result"]
    span_id: str
    payload: CommandResultPayload


class ListCapabilitiesEnvelope(SessionEnvelope):
    type: Literal["list_capabilities"]
    payload: EmptyPayload


class ManifestEnvelope(SessionEnvelope):
    type: Literal["manifest"]
    payload: Manifest


class EstopEnvelope(SessionEnvelope):
    type: Literal["estop"]
    payload: EstopPayload


class EstopClearEnvelope(SessionEnvelope):
    type: Literal["estop_clear"]
    payload: EstopClearPayload


class ErrorEnvelope(SessionEnvelope):
    type: Literal["error"]
    payload: ErrorPayload


Message = Annotated[
    HelloEnvelope
    | WelcomeEnvelope
    | RejectEnvelope
    | HeartbeatEnvelope
    | EventEnvelope
    | CommandEnvelope
    | CommandResultEnvelope
    | ListCapabilitiesEnvelope
    | ManifestEnvelope
    | EstopEnvelope
    | EstopClearEnvelope
    | ErrorEnvelope,
    Field(discriminator="type"),
]

MESSAGE_ADAPTER: TypeAdapter[Message] = TypeAdapter(Message)

#: envelope type string -> model class, derived from the union so it cannot
#: fall out of step with it.
ENVELOPE_BY_TYPE: dict[str, type[BaseEnvelope]] = {
    envelope.model_fields["type"].annotation.__args__[0]: envelope  # type: ignore[union-attr]
    for envelope in Message.__origin__.__args__  # type: ignore[attr-defined]
}
