"""Replay: read a recorded session back and re-feed it (ADR-0005 point 4).

This is the read side of the flight recorder and the seed of V1 acceptance
test 3, "replay yesterday's session and the decisions come out identical".

Two things make replay trustworthy rather than approximate:

Order comes from the file-wide `index` written into every record, not from
timestamps. Timestamps have microsecond resolution and can collide, and a
replay that reorders two messages is not a replay.

Messages are decoded through the same boundary as live traffic, so a
recording that has been edited into something illegal is caught here rather
than halfway through a re-run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcap.reader import make_reader

from brain.recorder import SESSION_META_TOPIC, Direction
from wire import Manifest, Message, Timestamp, decode_object, encode, schema_id

log = logging.getLogger("brain.replay")


class ReplayError(ValueError):
    """The file is not a session this brain can reconstruct."""


@dataclass(frozen=True, slots=True)
class SessionMeta:
    """The `session_meta` record: what the session was negotiated as."""

    session: str
    body_id: str
    protocol_version: str
    schema_id: str
    manifest: Manifest
    opened_utc: str


@dataclass(frozen=True, slots=True)
class RecordedMessage:
    """One message as it was recorded, decoded back into a model."""

    index: int
    direction: Direction
    message: Message
    t_captured: Timestamp
    t_received: Timestamp | None
    session: str | None
    body_id: str | None

    @property
    def type(self) -> str:
        return self.message.type

    def frame(self) -> str:
        """Re-encode exactly as it went on the wire."""
        return encode(self.message)


@dataclass(frozen=True, slots=True)
class ReplaySession:
    """A recorded session, in order, ready to re-feed."""

    path: Path
    meta: SessionMeta | None
    records: tuple[RecordedMessage, ...]

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[RecordedMessage]:
        return iter(self.records)

    def messages(self) -> tuple[Message, ...]:
        return tuple(record.message for record in self.records)

    def frames(self) -> tuple[str, ...]:
        """Every message re-encoded, in order. The comparison for acceptance
        test 3: these bytes must equal what the session originally sent."""
        return tuple(record.frame() for record in self.records)

    def inbound(self) -> tuple[RecordedMessage, ...]:
        return tuple(record for record in self.records if record.direction == "rx")

    def outbound(self) -> tuple[RecordedMessage, ...]:
        return tuple(record for record in self.records if record.direction == "tx")

    def feed(self, handler: Callable[[RecordedMessage], Any]) -> None:
        """Re-feed the session in order.

        Deliberately not time-paced. Replay is for reproducing decisions, and
        a decision that depends on how fast messages arrive is a bug the
        record should expose rather than reproduce.
        """
        for record in self.records:
            handler(record)


def read_session(path: Path | str) -> ReplaySession:
    """Load a recorded session from an MCAP file."""
    path = Path(path)

    meta: SessionMeta | None = None
    records: list[RecordedMessage] = []

    with path.open("rb") as handle:
        for _schema, channel, message in make_reader(handle).iter_messages():
            try:
                payload = json.loads(message.data)
            except ValueError as exc:
                raise ReplayError(f"{path}: record on {channel.topic} is not JSON: {exc}") from exc

            if channel.topic == SESSION_META_TOPIC:
                meta = _read_meta(payload, path)
            else:
                records.append(_read_message(payload, path))

    records.sort(key=lambda record: record.index)
    _check_indices(records, path)

    if meta is not None and meta.schema_id != schema_id():
        # SPEC section 9.4: sessions replay under the version they were
        # recorded with. Worth saying out loud rather than silently
        # reinterpreting old bytes under a newer contract.
        log.warning(
            "%s was recorded under schema %s; this brain runs %s",
            path,
            meta.schema_id,
            schema_id(),
        )

    return ReplaySession(path=path, meta=meta, records=tuple(records))


def _read_meta(payload: dict[str, Any], path: Path) -> SessionMeta:
    try:
        return SessionMeta(
            session=payload["session"],
            body_id=payload["body_id"],
            protocol_version=payload["protocol_version"],
            schema_id=payload["schema_id"],
            manifest=Manifest.model_validate(payload["manifest"]),
            opened_utc=payload["opened_utc"],
        )
    except (KeyError, ValueError) as exc:
        raise ReplayError(f"{path}: unusable session_meta record: {exc}") from exc


def _read_message(payload: dict[str, Any], path: Path) -> RecordedMessage:
    try:
        envelope = payload["message"]
        index = payload["index"]
        direction = payload["direction"]
    except KeyError as exc:
        raise ReplayError(f"{path}: record is missing {exc}") from exc

    # Through the same boundary as live traffic: an edited recording is
    # caught here, not halfway through a re-run.
    message = decode_object(envelope)

    received = payload.get("t_received")

    return RecordedMessage(
        index=index,
        direction=direction,
        message=message,
        t_captured=Timestamp.model_validate(payload["t_captured"]),
        t_received=Timestamp.model_validate(received) if received is not None else None,
        session=payload.get("session"),
        body_id=payload.get("body_id"),
    )


def _check_indices(records: list[RecordedMessage], path: Path) -> None:
    """Every index must be distinct. A duplicate means the order is a guess,
    and a replay whose order is a guess is not a replay."""
    seen = {record.index for record in records}
    if len(seen) != len(records):
        raise ReplayError(f"{path}: duplicate record indices, order cannot be reconstructed")
