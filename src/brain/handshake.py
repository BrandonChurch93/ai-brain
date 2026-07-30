"""Session opening: authenticate, negotiate a version, welcome or reject.

Kept free of sockets so the rules can be tested directly, and so the server
in `brain.server` is only plumbing (SPEC section 5).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from wire import (
    HelloEnvelope,
    Message,
    RejectEnvelope,
    RejectPayload,
    WelcomeEnvelope,
    WelcomePayload,
)
from wire.models import CapabilityAck, HeartbeatConfig, NamedVersion
from wire.schema import supported_versions
from wire.stamp import SeqCounter, new_id, new_session_id, now

#: Name the brain reports in `welcome.server`. A domain term, never the
#: project name (ADR-0000 rule 2).
SERVER_NAME = "brain"


@dataclass(frozen=True, slots=True)
class Accepted:
    """The handshake succeeded. `welcome` is ready to send."""

    welcome: WelcomeEnvelope
    session: str
    protocol_version: str
    body_id: str


@dataclass(frozen=True, slots=True)
class Refused:
    """The handshake failed. Send `reject`, then close (SPEC section 6.3)."""

    reject: RejectEnvelope
    reason: str


Outcome = Accepted | Refused


def choose_version(offered: list[str], supported: tuple[str, ...]) -> str | None:
    """The newest version both sides support (SPEC section 5.2).

    The body lists its versions newest first, but that is the body's claim
    about its own ordering, not something to trust for choosing. Version
    strings are dates, so sorting them descending is well defined and does
    not depend on the body having ordered its list honestly.
    """
    common = set(offered) & set(supported)
    if not common:
        return None
    return max(common)


def authenticate(presented: str, expected: str) -> bool:
    """Constant-time comparison (SPEC section 5).

    `compare_digest` so the time taken does not reveal how much of the token
    was correct.
    """
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def open_session(
    hello: HelloEnvelope,
    *,
    auth_token: str,
    heartbeat_interval_ms: int,
    heartbeat_lease_ms: int,
    server_version: str,
    seq: SeqCounter,
    supported: tuple[str, ...] | None = None,
    utc_now: str | None = None,
) -> Outcome:
    """Decide whether to welcome this body.

    Authentication is checked before version negotiation. SPEC section 5
    presents them the other way round, and either order satisfies the
    contract, because the two cases only overlap when a body is both
    unauthenticated and incompatible. Doing auth first means an unauthorised
    caller learns nothing about which protocol versions this brain speaks.
    """
    versions = supported_versions() if supported is None else supported

    if not authenticate(hello.payload.auth_token, auth_token):
        return _refuse(
            code="auth_failed",
            message="authentication failed",
            seq=seq,
            reason=f"bad token from body {hello.payload.manifest.body_id!r}",
        )

    chosen = choose_version(hello.payload.protocol_versions, versions)
    if chosen is None:
        return _refuse(
            code="unsupported_version",
            message=(
                f"no common protocol version; body offered "
                f"{', '.join(hello.payload.protocol_versions) or '(none)'}"
            ),
            seq=seq,
            reason="version negotiation failed",
            supported=list(versions),
        )

    session = new_session_id()
    stamp = now()
    declared = [capability.id for capability in hello.payload.manifest.capabilities]

    welcome = WelcomeEnvelope(
        type="welcome",
        id=new_id(),
        session=session,
        seq=seq.take(),
        ts=stamp,
        payload=WelcomePayload(
            protocol_version=chosen,
            session=session,
            server=NamedVersion(name=SERVER_NAME, version=server_version),
            # A clockless microcontroller seeds its wall clock from this.
            utc_now=stamp.utc if utc_now is None else utc_now,
            heartbeat=HeartbeatConfig(
                interval_ms=heartbeat_interval_ms,
                lease_ms=heartbeat_lease_ms,
            ),
            # Every declared capability is recognised: the class registry is
            # in the schema and the message already validated against it, so
            # an unknown class could not have reached here. `unrecognized`
            # earns its keep once the registry outgrows the schema.
            capability_ack=CapabilityAck(recognized=declared, unrecognized=[]),
        ),
    )

    return Accepted(
        welcome=welcome,
        session=session,
        protocol_version=chosen,
        body_id=hello.payload.manifest.body_id,
    )


def malformed_hello(message: str, seq: SeqCounter) -> Refused:
    """The first frame was not a usable `hello` (SPEC section 6.3)."""
    return _refuse(code="malformed_hello", message=message, seq=seq, reason=message)


def _refuse(
    *,
    code: str,
    message: str,
    seq: SeqCounter,
    reason: str,
    supported: list[str] | None = None,
) -> Refused:
    # `supported` is only set when there is a list to send. Passing None
    # explicitly would mark the field set, and serialization keeps set
    # fields, so the message would carry `"supported": null` where the schema
    # requires an array.
    payload_fields: dict[str, object] = {"code": code, "message": message}
    if supported is not None:
        payload_fields["supported"] = supported

    reject = RejectEnvelope(
        type="reject",
        id=new_id(),
        seq=seq.take(),
        ts=now(),
        payload=RejectPayload(**payload_fields),  # type: ignore[arg-type]
    )
    return Refused(reject=reject, reason=reason)


def is_hello(message: Message) -> bool:
    return isinstance(message, HelloEnvelope)
