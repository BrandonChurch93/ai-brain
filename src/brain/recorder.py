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
from wire.clock import SYSTEM_CLOCK, Clock
from wire.stamp import epoch_ns, now

log = logging.getLogger("brain.recorder")

Direction = Literal["rx", "tx"]

#: Channel carrying one record per session, written at `welcome`.
SESSION_META_TOPIC = "/session_meta"

#: Channel carrying every exchange with a language model (ADR-0005 point 1,
#: ADR-0007 point 4). Defined now and populated in Phase 5, so the shape is
#: settled before anything depends on it and old sessions stay readable.
LLM_IO_TOPIC = "/llm_io"

PROTOCOL_SCHEMA_NAME = "body-adapter-protocol"
SESSION_META_SCHEMA_NAME = "session_meta"
LLM_IO_SCHEMA_NAME = "llm_io"

#: The roles config routes models to (ADR-0007 point 2). Not an enum in the
#: schema: the routing table grows, and a log that refuses to record a role
#: it has not met is a log that loses exactly the novel event worth keeping.
LLM_ROLES = ("planner", "conversation", "vision", "classifier", "reflex")

#: Shape of a `session_meta` record. A log construct, not wire format, so it
#: is defined here rather than in `protocol/schemas/`.
SESSION_META_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Session metadata",
    "type": "object",
    "required": [
        "index",
        "session",
        "body_id",
        "protocol_version",
        "schema_id",
        "manifest",
        "opened_utc",
    ],
    "properties": {
        "index": {"type": "integer", "description": "File-wide write order."},
        "session": {"type": "string"},
        "body_id": {"type": "string"},
        "protocol_version": {"type": "string"},
        "schema_id": {"type": "string"},
        "manifest": {"type": "object"},
        "opened_utc": {"type": "string"},
    },
}


#: Shape of an `llm_io` record: one request and its response.
#:
#: Prompt and response are stored whole, never truncated. ADR-0005 exists so
#: "why did it do that?" is answerable, and a summarised prompt cannot answer
#: it. Replay also re-feeds recorded model output rather than re-buying it
#: (ADR-0005 point 4), which only works if what was recorded is complete.
LLM_IO_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "LLM exchange",
    "type": "object",
    "required": [
        "index",
        "role",
        "provider",
        "model",
        "prompt",
        "response",
        "tokens",
        "latency_ms",
        "t_request",
        "t_response",
    ],
    "properties": {
        "index": {"type": "integer", "description": "File-wide write order."},
        "session": {"type": ["string", "null"]},
        "trace_id": {
            "type": ["string", "null"],
            "description": "The goal this call served (ADR-0005 point 3).",
        },
        "span_id": {"type": ["string", "null"]},
        "role": {
            "type": "string",
            "description": "Routing role: planner, conversation, vision, classifier, reflex.",
        },
        "provider": {"type": "string"},
        "model": {"type": "string"},
        "prompt": {"description": "The request as sent, whole. Shape is provider-specific."},
        "response": {"description": "The response as received, whole."},
        "tokens": {
            "type": "object",
            "description": "Cost telemetry per decision (ADR-0007 point 4).",
            "properties": {
                "prompt": {"type": ["integer", "null"]},
                "completion": {"type": ["integer", "null"]},
                "total": {"type": ["integer", "null"]},
            },
        },
        "latency_ms": {"type": "number", "minimum": 0},
        "t_request": {"type": "object"},
        "t_response": {"type": "object"},
        "status": {
            "type": "string",
            "description": "ok, error, or refusal. A refusal is a real outcome, not a failure.",
        },
        "error": {"type": ["object", "null"]},
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

    def __init__(self, path: Path | str, clock: Clock = SYSTEM_CLOCK) -> None:
        self._path = Path(path)
        self._clock = clock
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

        llm = writer.register_schema(
            name=LLM_IO_SCHEMA_NAME,
            encoding="jsonschema",
            data=json.dumps(LLM_IO_SCHEMA).encode("utf-8"),
        )
        self._channels[LLM_IO_TOPIC] = writer.register_channel(
            topic=LLM_IO_TOPIC,
            message_encoding="json",
            schema_id=llm,
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
        stamp = now(self._clock) if opened_at is None else opened_at

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

    def record_llm_io(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        prompt: Any,
        response: Any,
        latency_ms: float,
        t_request: Timestamp,
        t_response: Timestamp,
        tokens: dict[str, int | None] | None = None,
        session: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        status: str = "ok",
        error: dict[str, Any] | None = None,
    ) -> None:
        """Log one exchange with a language model.

        The channel is defined now and used from Phase 5. Writing it here
        rather than later means the shape is settled before anything depends
        on it, and a session recorded today stays readable then.
        """
        self._write(
            LLM_IO_TOPIC,
            {
                "session": session,
                "trace_id": trace_id,
                "span_id": span_id,
                "role": role,
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "response": response,
                "tokens": tokens if tokens is not None else {},
                "latency_ms": latency_ms,
                "t_request": {"mono_ns": t_request.mono_ns, "utc": t_request.utc},
                "t_response": {"mono_ns": t_response.mono_ns, "utc": t_response.utc},
                "status": status,
                "error": error,
            },
            log_time=epoch_ns(t_response),
            publish_time=epoch_ns(t_request),
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

        # Inbound traffic was received; outbound traffic was not, and
        # inventing a t_received for it would be a lie in the log.
        received = (t_received or now(self._clock)) if direction == "rx" else None

        record: dict[str, Any] = {
            "direction": direction,
            "session": session if session is not None else getattr(message, "session", None),
            "body_id": body_id,
            "type": message.type,
            "seq": message.seq,
            "trace_id": message.trace_id,
            "span_id": message.span_id,
            "t_captured": {"mono_ns": captured.mono_ns, "utc": captured.utc},
            "t_received": {"mono_ns": received.mono_ns, "utc": received.utc}
            if received is not None
            else None,
            "message": to_object(message),
        }

        # log_time is when this recorder wrote the record, publish_time is
        # when the sender created the message. Using the message's own stamp
        # as log_time looks equivalent for freshly sent traffic and is not:
        # recording anything built earlier walks log_time backwards, which
        # `mcap doctor` rightly complains about and which breaks the time
        # index readers rely on.
        logged = received if received is not None else now(self._clock)

        self._write(
            topic_for(message.type),
            record,
            log_time=epoch_ns(logged),
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

        # A file-wide write order, so replay reconstructs the exact sequence.
        # log_time cannot carry that on its own: it comes from an ISO
        # timestamp with microsecond resolution, and two records written
        # inside the same microsecond would be indistinguishable. Replay
        # being deterministic is the whole point of ADR-0005.
        self._count += 1
        record = {"index": self._count, **record}

        self._writer.add_message(
            channel_id=channel,
            log_time=log_time,
            publish_time=publish_time,
            sequence=sequence,
            data=json.dumps(record, separators=(",", ":")).encode("utf-8"),
        )
