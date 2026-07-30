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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

import websockets
from websockets.asyncio.server import Server, ServerConnection, serve

from brain.config import ServerConfig
from brain.handshake import Accepted, Refused, is_hello, malformed_hello, open_session
from wire import (
    SUBPROTOCOL,
    MalformedFrameError,
    Message,
    ProtocolValidationError,
    decode,
    encode,
)
from wire.stamp import SeqCounter

log = logging.getLogger("brain.server")

#: Called with (session, message) for every validated inbound message after
#: the handshake. Phase 1.3 replaces this seam with the session registry.
MessageHandler = Callable[["SessionState", Message], Awaitable[None]]


def _server_version() -> str:
    try:
        return package_version("brain")
    except PackageNotFoundError:  # running from a source tree without install
        return "0.0.0+unknown"


@dataclass(slots=True)
class SessionState:
    """One accepted connection, from `welcome` to socket close."""

    session: str
    body_id: str
    protocol_version: str
    connection: ServerConnection
    outbound_seq: SeqCounter = field(default_factory=SeqCounter)

    async def send(self, message: Message) -> None:
        await self.connection.send(encode(message))


class BrainServer:
    """Accepts bodies, runs the handshake, then hands messages to a handler."""

    def __init__(
        self,
        config: ServerConfig,
        *,
        on_message: MessageHandler | None = None,
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._server: Server | None = None
        self._sessions: dict[str, SessionState] = {}

    @property
    def sessions(self) -> dict[str, SessionState]:
        return dict(self._sessions)

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

        try:
            await self._steady_state(state)
        finally:
            self._sessions.pop(state.session, None)
            log.info("session %s closed", state.session)

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
        return SessionState(
            session=outcome.session,
            body_id=outcome.body_id,
            protocol_version=outcome.protocol_version,
            connection=connection,
            outbound_seq=seq,
        )

    async def _refuse(self, connection: ServerConnection, refused: Refused) -> None:
        log.info("rejecting body: %s (%s)", refused.reason, refused.reject.payload.code)
        with contextlib.suppress(websockets.exceptions.ConnectionClosed):
            await connection.send(encode(refused.reject))
            await connection.close()
        return None

    async def _steady_state(self, state: SessionState) -> None:
        """Receive and validate. Heartbeats, sequencing, and dispatch arrive
        in checklist steps 1.3 and 1.4."""
        try:
            async for raw in state.connection:
                try:
                    message = decode(raw)
                except (MalformedFrameError, ProtocolValidationError) as exc:
                    log.warning("session %s sent an invalid message: %s", state.session, exc)
                    continue

                if self._on_message is not None:
                    await self._on_message(state, message)
        except websockets.exceptions.ConnectionClosed:
            return
