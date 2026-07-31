"""The body side of the protocol: connect, handshake, heartbeat, dispatch.

Every body is a client. It dials the brain, presents a manifest, and from
then on answers commands and emits events. Nothing here knows what a mock
body or a laptop body is; that lives in the adapters built on top, which is
what makes adding a body a change outside `brain/` (ADR-0001).

Time is injected, like everywhere else. A body's timers are safety timers,
and step 3.3 turns this into latching that tests must be able to drive
deterministically.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from wire import (
    SUBPROTOCOL,
    EventEnvelope,
    EventPayload,
    HeartbeatEnvelope,
    HeartbeatPayload,
    HelloEnvelope,
    HelloPayload,
    MalformedFrameError,
    Manifest,
    Message,
    ProtocolValidationError,
    RejectEnvelope,
    Timestamp,
    WelcomeEnvelope,
    decode,
    encode,
)
from wire.clock import SYSTEM_CLOCK, Clock
from wire.models import without_none
from wire.schema import supported_versions
from wire.stamp import SeqCounter, new_id, now

log = logging.getLogger("bodies.client")

#: The system capability every body must present, with id `sys`
#: (SPEC section 7.2).
SYS = "sys"

#: Reserved event on every body, emitted on each state transition
#: (SPEC section 6.5).
STATE_EVENT = "state"

Sleeper = Callable[[float], Awaitable[None]]
MessageHandler = Callable[["BodyClient", Message, Timestamp], Awaitable[None]]


class HandshakeRejected(RuntimeError):
    """The brain refused this body (SPEC section 6.3)."""

    def __init__(self, reject: RejectEnvelope) -> None:
        payload = reject.payload
        super().__init__(f"{payload.code}: {payload.message}")
        self.code = payload.code
        self.supported = payload.supported


@dataclass(frozen=True, slots=True)
class BodyConfig:
    """Body-side configuration. The brain URL lives here, never brain-side
    (SPEC section 3.1)."""

    url: str
    auth_token: str = ""
    #: Versions this body speaks, newest first (SPEC section 5.1).
    protocol_versions: tuple[str, ...] = field(default_factory=supported_versions)
    #: How long to wait for `welcome` before giving up.
    handshake_timeout_s: float = 10.0


class BodyClient:
    """One body, one connection, one session.

    Owns the session-scoped state that resets on reconnect: sequence numbers
    and the negotiated heartbeat interval. What must *not* reset on reconnect
    is the body's safety state, which is why that is held by the adapter and
    passed in rather than being reset here (SPEC section 5, reconnection).
    """

    def __init__(
        self,
        manifest: Manifest,
        config: BodyConfig,
        *,
        on_message: MessageHandler | None = None,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._manifest = manifest
        self._config = config
        self._on_message = on_message
        self._clock = clock
        self._sleep = sleep

        self._connection: ClientConnection | None = None
        self._seq = SeqCounter()
        self._session: str | None = None
        self._protocol_version: str | None = None
        self._interval_ms: int | None = None
        self._lease_ms: int | None = None
        self._state: str = manifest.boot_state
        self._tasks: list[asyncio.Task[Any]] = []

    # What the body knows about itself

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    @property
    def body_id(self) -> str:
        return self._manifest.body_id

    @property
    def session(self) -> str | None:
        return self._session

    @property
    def protocol_version(self) -> str | None:
        return self._protocol_version

    @property
    def heartbeat_interval_ms(self) -> int | None:
        return self._interval_ms

    @property
    def heartbeat_lease_ms(self) -> int | None:
        return self._lease_ms

    @property
    def state(self) -> str:
        return self._state

    @property
    def connected(self) -> bool:
        return self._session is not None

    @property
    def clock(self) -> Clock:
        return self._clock

    def next_seq(self) -> int:
        """Take the next outbound sequence number (SPEC section 4)."""
        return self._seq.take()

    def stamp(self) -> Timestamp:
        """A timestamp on this body's clock."""
        return now(self._clock)

    async def send(self, message: Message) -> None:
        """Send an already-built message. Validated at the boundary."""
        await self._send(message)

    # Session lifecycle

    async def connect(self) -> WelcomeEnvelope:
        """Open a session: hello, then welcome or reject (SPEC section 5)."""
        self._connection = await connect(
            self._config.url,
            subprotocols=[SUBPROTOCOL],  # type: ignore[list-item]
        )

        await self._send(self._hello())

        raw = await asyncio.wait_for(
            self._connection.recv(), timeout=self._config.handshake_timeout_s
        )
        reply = decode(raw)

        if isinstance(reply, RejectEnvelope):
            await self._connection.close()
            self._connection = None
            raise HandshakeRejected(reply)

        if not isinstance(reply, WelcomeEnvelope):
            await self._connection.close()
            self._connection = None
            raise ProtocolValidationError(f"expected welcome or reject, got {reply.type!r}", [])

        self._session = reply.payload.session
        self._protocol_version = reply.payload.protocol_version
        self._interval_ms = reply.payload.heartbeat.interval_ms
        self._lease_ms = reply.payload.heartbeat.lease_ms

        log.info(
            "body %s: session %s open on protocol %s",
            self.body_id,
            self._session,
            self._protocol_version,
        )
        return reply

    async def announce_boot_state(self) -> None:
        """Emit the reserved `sys` state event for the state booted into.

        SPEC section 7.1: a body with actuation boots into `safe_hold`, and
        section 6.5 says every state transition is announced. Boot is the
        first one, and skipping it would leave the brain guessing at a
        body's state until something else happened to reveal it.
        """
        await self.emit_state(self._state, cause="boot")

    async def emit_state(self, state: str, *, cause: str) -> None:
        """Announce a body-state transition (SPEC section 6.5)."""
        self._state = state
        await self.emit(SYS, STATE_EVENT, {"state": state, "cause": cause}, droppable=False)

    async def emit(
        self,
        capability: str,
        event: str,
        data: dict[str, Any],
        *,
        droppable: bool = False,
        trace_id: str | None = None,
    ) -> None:
        """Send one event (SPEC section 6.5)."""
        await self._send(
            EventEnvelope(
                **without_none(
                    type="event",
                    id=new_id(),
                    session=self._session,
                    seq=self._seq.take(),
                    ts=now(self._clock),
                    trace_id=trace_id,
                    payload=EventPayload(
                        capability=capability,
                        event=event,
                        data=data,
                        droppable=droppable,
                    ),
                )
            )
        )

    async def send_heartbeat(self) -> None:
        """One heartbeat carrying the body's current state (SPEC section 6.4)."""
        await self._send(
            HeartbeatEnvelope(
                type="heartbeat",
                id=new_id(),
                session=self._session,  # type: ignore[arg-type]
                seq=self._seq.take(),
                ts=now(self._clock),
                payload=HeartbeatPayload(state=self._state),  # type: ignore[arg-type]
            )
        )

    async def run(self) -> None:
        """Handshake, announce boot state, then serve until the socket closes."""
        await self.connect()
        await self.announce_boot_state()
        try:
            await self.run_loops()
        finally:
            await self.close()

    async def run_loops(self) -> None:
        """Heartbeat and receive, until the socket closes.

        Separate from `run` so an adapter can own the order of connect,
        announce, and its own startup work, and still hand the steady state
        back here.
        """
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._receive_loop()),
        ]
        await asyncio.gather(*self._tasks)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []

        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close()
        self._connection = None
        self._session = None

    # Internals

    def _hello(self) -> HelloEnvelope:
        return HelloEnvelope(
            type="hello",
            id=new_id(),
            seq=self._seq.take(),
            ts=now(self._clock),
            payload=HelloPayload(
                protocol_versions=list(self._config.protocol_versions),
                auth_token=self._config.auth_token,
                manifest=self._manifest,
            ),
        )

    async def _send(self, message: Message) -> None:
        if self._connection is None:
            raise RuntimeError(f"body {self.body_id} is not connected")
        await self._connection.send(encode(message))

    async def _heartbeat_loop(self) -> None:
        """Beat on the interval the brain named in `welcome`."""
        interval_s = (self._interval_ms or 1000) / 1000

        while True:
            await self._sleep(interval_s)
            try:
                await self.send_heartbeat()
            except websockets.exceptions.ConnectionClosed:
                return
            except Exception:
                log.exception("body %s: heartbeat send failed", self.body_id)

    async def _receive_loop(self) -> None:
        assert self._connection is not None

        try:
            async for raw in self._connection:
                # Stamped before parsing. A command's TTL is measured from
                # the body's receipt, so the clock must start at the wire and
                # not after validation (SPEC section 6.6).
                received_at = now(self._clock)

                try:
                    message = decode(raw)
                except (MalformedFrameError, ProtocolValidationError) as exc:
                    log.warning("body %s: invalid message from brain: %s", self.body_id, exc)
                    continue

                if self._on_message is not None:
                    await self._on_message(self, message, received_at)
        except websockets.exceptions.ConnectionClosed:
            return
