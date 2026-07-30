"""The flight recorder: every message to and from a body, in MCAP (ADR-0005).

One channel per protocol message type, plus `session_meta` carrying the
negotiated version and the body's manifest. Splitting by type is what makes
the file useful in Foxglove without a custom tool: heartbeats can be hidden
and commands followed.

Direction lives in the record rather than in the channel, so a span's
command and its results sit on the streams their types say they do and are
still distinguishable.

The protocol schema is embedded in the file. An MCAP session is then
self-describing: whoever opens it later gets the contract it was recorded
under, without needing this repo at the version that wrote it (SPEC section
9.4, replaying under the version recorded).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from mcap.writer import Writer

from wire import Manifest, Message, Timestamp, message_types, protocol_schema, schema_id, to_object
from wire.stamp import epoch_ns, now

log = logging.getLogger("brain.recorder")

Direction = Literal["rx", "tx"]

#: Channel carrying one record per session, written at `welcome`.
SESSION_META_TOPIC = "/session_meta"

PROTOCOL_SCHEMA_NAME = "body-adapter-protocol"
SESSION_META_SCHEMA_NAME = "session_meta"

#: Shape of a `session_meta` record. A log construct, not wire format, so it
#: is defined here rather than in `protocol/schemas/`.
SESSION_META_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Session metadata",
    "type": "object",
    "required": ["session", "body_id", "protocol_version", "schema_id", "manifest", "opened_utc"],
    "properties": {
        "session": {"type": "string"},
        "body_id": {"type": "string"},
        "protocol_version": {"type": "string"},
        "schema_id": {"type": "string"},
        "manifest": {"type": "object"},
        "opened_utc": {"type": "string"},
    },
}


def topic_for(message_type: str) -> str:
    return f"/{message_type}"


@dataclass(frozen=True, slots=True)
class RecorderStats:
    channels: int
    messages: int


class FlightRecorder:
    """Writes one MCAP file per session.

    Every channel is registered up front rather than on first use, so an
    opened file shows which streams exist even when nothing has arrived on
    them. A missing channel and a silent one mean different things.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._stream: Any = None
        self._writer: Writer | None = None
        self._channels: dict[str, int] = {}
        self._sequence: dict[str, int] = {}
        self._count = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def message_count(self) -> int:
        return self._count

    @property
    def channel_count(self) -> int:
        return len(self._channels)

    def open(self) -> Self:
        if self._writer is not None:
            raise RuntimeError(f"recorder for {self._path} is already open")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self._path.open("wb")

        writer = Writer(self._stream)
        # The profile names the domain this file belongs to. Generic, never
        # the project name (ADR-0000 rule 5).
        writer.start(profile="body-adapter-protocol")

        protocol = writer.register_schema(
            name=PROTOCOL_SCHEMA_NAME,
            encoding="jsonschema",
            data=json.dumps(protocol_schema()).encode("utf-8"),
        )
        meta = writer.register_schema(
            name=SESSION_META_SCHEMA_NAME,
            encoding="jsonschema",
            data=json.dumps(SESSION_META_SCHEMA).encode("utf-8"),
        )

        for message_type in sorted(message_types()):
            topic = topic_for(message_type)
            self._channels[topic] = writer.register_channel(
                topic=topic,
                message_encoding="json",
                schema_id=protocol,
                metadata={"message_type": message_type},
            )

        self._channels[SESSION_META_TOPIC] = writer.register_channel(
            topic=SESSION_META_TOPIC,
            message_encoding="json",
            schema_id=meta,
        )

        self._writer = writer
        return self

    def close(self) -> RecorderStats:
        if self._writer is None:
            raise RuntimeError(f"recorder for {self._path} is not open")

        self._writer.finish()
        self._stream.close()
        self._writer = None
        self._stream = None

        return RecorderStats(channels=len(self._channels), messages=self._count)

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def record_session(
        self,
        *,
        session: str,
        body_id: str,
        protocol_version: str,
        manifest: Manifest,
        opened_at: Timestamp | None = None,
    ) -> None:
        """One `session_meta` record, written when the session opens."""
        stamp = now() if opened_at is None else opened_at

        self._write(
            SESSION_META_TOPIC,
            {
                "session": session,
                "body_id": body_id,
                "protocol_version": protocol_version,
                "schema_id": schema_id(),
                "manifest": manifest.model_dump(by_alias=True, exclude_unset=True, mode="json"),
                "opened_utc": stamp.utc,
            },
            log_time=epoch_ns(stamp),
            publish_time=epoch_ns(stamp),
        )

    def record(
        self,
        direction: Direction,
        message: Message,
        *,
        session: str | None = None,
        body_id: str | None = None,
        t_received: Timestamp | None = None,
    ) -> None:
        """Log one message in either direction.

        `t_captured` comes off the message; `t_received` is the receiver's
        own stamp and only exists for inbound traffic. Both are kept, along
        with `seq`, `trace_id`, and `span_id` hoisted to the top level, so a
        reader can filter on them without parsing the nested envelope
        (ADR-0005 point 2 and 3).
        """
        captured = message.ts
        stamp = t_received if t_received is not None else (now() if direction == "rx" else captured)

        record: dict[str, Any] = {
            "direction": direction,
            "session": session if session is not None else getattr(message, "session", None),
            "body_id": body_id,
            "type": message.type,
            "seq": message.seq,
            "trace_id": message.trace_id,
            "span_id": message.span_id,
            "t_captured": {"mono_ns": captured.mono_ns, "utc": captured.utc},
            "t_received": {"mono_ns": stamp.mono_ns, "utc": stamp.utc}
            if direction == "rx"
            else None,
            "message": to_object(message),
        }

        self._write(
            topic_for(message.type),
            record,
            # log_time is when this recorder saw it, publish_time is when the
            # sender made it. For outbound traffic those are the same event.
            log_time=epoch_ns(stamp),
            publish_time=epoch_ns(captured),
        )

    def _write(
        self,
        topic: str,
        record: dict[str, Any],
        *,
        log_time: int,
        publish_time: int,
    ) -> None:
        if self._writer is None:
            raise RuntimeError(f"recorder for {self._path} is not open")

        channel = self._channels.get(topic)
        if channel is None:
            raise KeyError(f"no channel for topic {topic!r}")

        sequence = self._sequence.get(topic, 0) + 1
        self._sequence[topic] = sequence

        self._writer.add_message(
            channel_id=channel,
            log_time=log_time,
            publish_time=publish_time,
            sequence=sequence,
            data=json.dumps(record, separators=(",", ":")).encode("utf-8"),
        )
        self._count += 1
