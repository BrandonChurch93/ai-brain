"""The brain as a WebSocket server (ADR-0002, SPEC sections 3, 5, 6).

Bodies dial in; the brain never dials out. One connection per body, one
session per connection.

The subprotocol is enforced at the HTTP upgrade by the `websockets` library:
declaring `subprotocols` makes it answer 400 both when a client offers none
and when none match, which is what SPEC section 3.2 requires. There is no
custom negotiation here because none is needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

import websockets
from websockets.asyncio.server import Server, ServerConnection, serve

from brain.config import ServerConfig
from brain.handshake import Accepted, Refused, is_hello, malformed_hello, open_session
from brain.heartbeat import BrainState, LeaseWatch, heartbeat_loop
from brain.recorder import FlightRecorder
from brain.registry import Reception, SessionRecord, SessionRegistry, SpanOutcome
from wire import (
    SUBPROTOCOL,
    HeartbeatEnvelope,
    MalformedFrameError,
    Message,
    ProtocolValidationError,
    decode,
    encode,
)
from wire.stamp import SeqCounter, now

log = logging.getLogger("brain.server")

#: Called with (session, reception) for every validated inbound message after
#: the handshake, carrying the brain's own t_received and what the sequence
#: number said about it.
MessageHandler = Callable[["SessionState", Reception], Awaitable[None]]

#: Called when a body's heartbeat lease expires, with the spans the brain
#: gave up on as a result.
LostHandler = Callable[["SessionState", list[SpanOutcome]], Awaitable[None]]


def _server_version() -> str:
    try:
        return package_version("brain")
    except PackageNotFoundError:  # running from a source tree without install
        return "0.0.0+unknown"


@dataclass(slots=True)
class SessionState:
    """A registry record plus the socket it belongs to.

    The registry stays socket-free so its bookkeeping can be tested without a
    network; this is where the two meet.
    """

    record: SessionRecord
    connection: ServerConnection
    #: Timing lives here rather than on the record, so the registry stays
    #: pure bookkeeping.
    lease: LeaseWatch | None = None

    @property
    def session(self) -> str:
        return self.record.session

    @property
    def body_id(self) -> str:
        return self.record.body_id

    @property
    def protocol_version(self) -> str:
        return self.record.protocol_version

    @property
    def outbound_seq(self) -> SeqCounter:
        return self.record.outbound

    async def send(self, message: Message) -> None:
        await self.connection.send(encode(message))


class BrainServer:
    """Accepts bodies, runs the handshake, then hands messages to a handler."""

    def __init__(
        self,
        config: ServerConfig,
        *,
        on_message: MessageHandler | None = None,
        on_lost: LostHandler | None = None,
        recorder: FlightRecorder | None = None,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._on_lost = on_lost
        self._recorder = recorder
        self._clock = clock
        self._server: Server | None = None
        self._registry = SessionRegistry()
        self._sessions: dict[str, SessionState] = {}
        self._brain_state: BrainState = "active"

    @property
    def brain_state(self) -> BrainState:
        """What the brain reports about itself in its heartbeats.

        `active` or `degraded` (SPEC section 6.4). The FSM that drives this
        properly is checklist step 5.4; for now it is settable.
        """
        return self._brain_state

    @brain_state.setter
    def brain_state(self, value: BrainState) -> None:
        self._brain_state = value

    @property
    def sessions(self) -> dict[str, SessionState]:
        return dict(self._sessions)

    @property
    def registry(self) -> SessionRegistry:
        return self._registry

    @property
    def port(self) -> int:
        """The bound port. Differs from config when the port was 0."""
        if self._server is None:
            raise RuntimeError("server is not running")
        return self._server.sockets[0].getsockname()[1]

    async def __aenter__(self) -> BrainServer:
        self._server = await serve(
            self._handle,
            self._config.host,
            self._config.port,
            subprotocols=[SUBPROTOCOL],  # type: ignore[list-item]
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, connection: ServerConnection) -> None:
        seq = SeqCounter()

        try:
            state = await self._open(connection, seq)
        except TimeoutError:
            log.info("body never sent hello within the handshake timeout; closing")
            return
        except websockets.exceptions.ConnectionClosed:
            return

        if state is None:
            return

        self._sessions[state.session] = state
        log.info(
            "session %s open: body %s on protocol %s",
            state.session,
            state.body_id,
            state.protocol_version,
        )

        beating = asyncio.create_task(self._beat(state))
        try:
            await self._steady_state(state)
        finally:
            beating.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beating
            self._sessions.pop(state.session, None)
            self._registry.close(state.session)
            log.info("session %s closed", state.session)

    async def _beat(self, state: SessionState) -> None:
        """Send heartbeats and watch this body's lease (SPEC section 8.1)."""
        assert state.lease is not None

        async def send(message: HeartbeatEnvelope) -> None:
            await state.connection.send(encode(message))
            self._record("tx", message, session=state.session, body_id=state.body_id)

        async def lost(silent_ms: float) -> None:
            outcomes = self._registry.mark_lost(
                state.session,
                message=f"no heartbeat for {silent_ms:.0f}ms; body considered lost",
            )
            if self._on_lost is not None:
                await self._on_lost(state, outcomes)

        async def recovered() -> None:
            self._registry.mark_live(state.session)

        await heartbeat_loop(
            session=state.session,
            interval_ms=self._config.heartbeat_interval_ms,
            watch=state.lease,
            seq=state.record.outbound,
            send=send,
            brain_state=lambda: self._brain_state,
            on_lost=lost,
            on_recovered=recovered,
        )

    async def _open(self, connection: ServerConnection, seq: SeqCounter) -> SessionState | None:
        """Run the handshake. Returns None if the body was rejected."""
        raw = await asyncio.wait_for(connection.recv(), timeout=self._config.handshake_timeout_s)

        try:
            first = decode(raw)
        except MalformedFrameError as exc:
            return await self._refuse(connection, malformed_hello(str(exc), seq))
        except ProtocolValidationError as exc:
            return await self._refuse(connection, malformed_hello(str(exc), seq))

        if not is_hello(first):
            return await self._refuse(
                connection,
                malformed_hello(f"first message was {first.type!r}, expected 'hello'", seq),
            )

        outcome = open_session(
            first,  # type: ignore[arg-type]
            auth_token=self._config.auth_token,
            heartbeat_interval_ms=self._config.heartbeat_interval_ms,
            heartbeat_lease_ms=self._config.heartbeat_lease_ms,
            server_version=_server_version(),
            seq=seq,
        )

        if isinstance(outcome, Refused):
            return await self._refuse(connection, outcome)

        assert isinstance(outcome, Accepted)
        await connection.send(encode(outcome.welcome))

        if self._recorder is not None:
            # Guarded like every other write: this one sits directly in the
            # handshake path, so an unguarded failure here would refuse a
            # body over a logging problem.
            try:
                self._recorder.record_session(
                    session=outcome.session,
                    body_id=outcome.body_id,
                    protocol_version=outcome.protocol_version,
                    manifest=outcome.manifest,
                )
            except Exception:
                log.exception("failed to record session_meta for %s", outcome.session)

            # hello arrived before the session existed, so it is recorded
            # here rather than in the receive loop. Leaving it out would
            # lose the manifest exchange the session was built on.
            self._record("rx", first, session=outcome.session, body_id=outcome.body_id)
            self._record("tx", outcome.welcome, session=outcome.session, body_id=outcome.body_id)

        record = self._registry.open(
            session=outcome.session,
            body_id=outcome.body_id,
            protocol_version=outcome.protocol_version,
            manifest=outcome.manifest,
            first_seq=outcome.first_seq,
            outbound=seq,
        )
        return SessionState(
            record=record,
            connection=connection,
            lease=LeaseWatch(self._config.heartbeat_lease_ms, self._clock),
        )

    async def _refuse(self, connection: ServerConnection, refused: Refused) -> None:
        log.info("rejecting body: %s (%s)", refused.reason, refused.reject.payload.code)
        self._record("tx", refused.reject)
        with contextlib.suppress(websockets.exceptions.ConnectionClosed):
            await connection.send(encode(refused.reject))
            await connection.close()
        return None

    def _record(
        self,
        direction: str,
        message: Message,
        *,
        session: str | None = None,
        body_id: str | None = None,
        t_received: object = None,
    ) -> None:
        """Record if there is a recorder, and never let logging break the run.

        A flight recorder that can take the aircraft down is worse than no
        flight recorder (ADR-0005).
        """
        if self._recorder is None:
            return
        try:
            self._recorder.record(
                direction,  # type: ignore[arg-type]
                message,
                session=session,
                body_id=body_id,
                t_received=t_received,  # type: ignore[arg-type]
            )
        except Exception:
            log.exception("failed to record a %s %s message", direction, message.type)

    async def _steady_state(self, state: SessionState) -> None:
        """Receive, stamp, record, dispatch. Heartbeats arrive in step 1.4."""
        try:
            async for raw in state.connection:
                # Stamped before parsing, so t_received measures the wire
                # rather than how long validation took (SPEC section 4).
                t_received = now()

                try:
                    message = decode(raw)
                except (MalformedFrameError, ProtocolValidationError) as exc:
                    log.warning("session %s sent an invalid message: %s", state.session, exc)
                    continue

                # SPEC section 8.1 words the lease in terms of heartbeats,
                # not traffic in general, so only a heartbeat renews it. A
                # body streaming frames while its heartbeat thread is wedged
                # is exactly the failure this is meant to catch.
                if isinstance(message, HeartbeatEnvelope) and state.lease is not None:
                    state.lease.beat()

                reception = self._registry.receive(state.session, message, t_received)
                self._record(
                    "rx",
                    message,
                    session=state.session,
                    body_id=state.body_id,
                    t_received=t_received,
                )

                if self._on_message is not None:
                    await self._on_message(state, reception)
        except websockets.exceptions.ConnectionClosed:
            return
