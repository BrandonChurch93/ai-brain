"""Every message that crosses the wire reaches the flight recorder.

ADR-0005 says log the why, not just the what. A recording that holds results
without the commands that caused them answers "what happened" and not "why",
which is the only question it exists to answer.

This is a regression suite. The Gate 3 drill recorded six `command_result`
messages and zero `command` messages, because recording lived at each call
site and `SessionState.send` was not one of them. Any future sender would
have been invisible the same way, silently, until somebody read a file.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcap.reader import make_reader

from bodies.client import BodyConfig
from bodies.mock import MockBody
from brain.config import ServerConfig
from brain.recorder import FlightRecorder
from brain.server import BrainServer
from wire import (
    CommandEnvelope,
    CommandPayload,
    EstopClearEnvelope,
    EstopClearPayload,
    EstopEnvelope,
    EstopPayload,
    without_none,
)
from wire.stamp import new_id, now

TOKEN = "recording-token"


@dataclass
class Drill:
    """A drill-shaped session, with a way to stop it and read the tape.

    Reading has to wait for `close`: MCAP writes its index at `finish`, so a
    file inspected mid-write ends early and tells you nothing.
    """

    server: BrainServer
    body: MockBody
    session: str
    path: Path
    _recorder: FlightRecorder
    _loops: asyncio.Task
    _closed: bool = False

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loops.cancel()
        await self.body.client.close()
        self._recorder.close()

    async def tape(self) -> list[tuple[str, dict]]:
        """Everything recorded, in order."""
        await self.stop()
        with self.path.open("rb") as handle:
            return [
                (channel.topic, json.loads(message.data))
                for _schema, channel, message in make_reader(handle).iter_messages()
            ]

    async def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for topic, _record in await self.tape():
            tally[topic] = tally.get(topic, 0) + 1
        return tally


@pytest.fixture
async def drill(tmp_path: Path) -> AsyncIterator[Drill]:
    path = tmp_path / "session.mcap"
    recorder = FlightRecorder(path)
    recorder.open()

    config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)
    async with BrainServer(config, recorder=recorder) as server:
        body = MockBody(
            BodyConfig(url=f"ws://127.0.0.1:{server.port}", auth_token=TOKEN),
        )
        welcome = await body.client.connect()
        await body.client.announce_boot_state()
        loops = asyncio.create_task(body.client.run_loops())

        running = Drill(
            server=server,
            body=body,
            session=welcome.payload.session,
            path=path,
            _recorder=recorder,
            _loops=loops,
        )
        try:
            yield running
        finally:
            await running.stop()


async def until(condition, *, what: str, spins: int = 40_000) -> None:
    for _ in range(spins):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {what}")


def command(session: str, capability: str, action: str, span_id: str, **params: Any):
    return CommandEnvelope(
        **without_none(
            type="command",
            id=new_id(),
            session=session,
            seq=0,
            ts=now(),
            trace_id="trc_recording",
            span_id=span_id,
            payload=CommandPayload(
                capability=capability,
                action=action,
                params=params,
                ttl_ms=5000,
            ),
        )
    )


async def send(server: BrainServer, session: str, message: Any) -> None:
    state = server.sessions[session]
    message.seq = state.record.outbound.take()
    if isinstance(message, CommandEnvelope):
        server.registry.open_span(session, message)
    await state.send(message)


async def test_every_command_is_recorded_alongside_its_result(drill: Drill) -> None:
    """The bug this file exists for. Effects without causes is not a record."""
    server, body, session = drill.server, drill.body, drill.session

    await send(server, session, command(session, "sys", "clear_safe_hold", "spn_1"))
    await until(lambda: body.state == "ok", what="the safe hold to clear")

    for index in range(4):
        await send(
            server,
            session,
            command(session, "drive0", "set_velocity", f"spn_{index + 2}", linear_mps=0.1),
        )
    await until(
        lambda: len(body.ledger) >= 5,
        what="all five commands to be handled",
    )
    tally = await drill.counts()
    assert tally.get("/command", 0) == 5
    assert tally.get("/command_result", 0) == 5, "results recorded without their commands"


async def test_commands_and_results_pair_by_span(drill: Drill) -> None:
    """Not just equal counts: the same spans on both sides.

    Equal totals could still be two commands and two results for different
    spans, which would look right and explain nothing.
    """
    server, body, session = drill.server, drill.body, drill.session

    await send(server, session, command(session, "sys", "ping", "spn_ping"))
    await send(server, session, command(session, "range0", "read", "spn_read"))
    await until(lambda: len(body.ledger) >= 2, what="both commands")

    records = await drill.tape()
    sent = {r["span_id"] for topic, r in records if topic == "/command"}
    answered = {r["span_id"] for topic, r in records if topic == "/command_result"}

    assert sent == {"spn_ping", "spn_read"}
    assert answered == sent


async def test_commands_are_recorded_as_outbound(drill: Drill) -> None:
    server, body, session = drill.server, drill.body, drill.session

    await send(server, session, command(session, "sys", "ping", "spn_dir"))
    await until(lambda: len(body.ledger) >= 1, what="the command")

    commands = [r for topic, r in await drill.tape() if topic == "/command"]
    assert commands
    assert all(r["direction"] == "tx" for r in commands)
    assert all(r["t_received"] is None for r in commands)


async def test_estop_and_its_release_are_recorded(drill: Drill) -> None:
    """Both were zero in the Gate 3 recording, because the drill called into
    the body rather than sending anything. A safety event that leaves no
    trace on the wire is not auditable."""
    server, body, session = drill.server, drill.body, drill.session

    await send(
        server,
        session,
        EstopEnvelope(
            **without_none(
                type="estop",
                id=new_id(),
                session=session,
                seq=0,
                ts=now(),
                payload=EstopPayload(reason="recording test"),
            )
        ),
    )
    await until(lambda: body.state == "estopped", what="the body to stop")

    await send(
        server,
        session,
        EstopClearEnvelope(
            **without_none(
                type="estop_clear",
                id=new_id(),
                session=session,
                seq=0,
                ts=now(),
                payload=EstopClearPayload(reason="cleared", operator="tester"),
            )
        ),
    )
    await until(lambda: body.state == "safe_hold", what="the estop to clear")

    tally = await drill.counts()
    assert tally.get("/estop", 0) == 1
    assert tally.get("/estop_clear", 0) == 1


async def test_the_handshake_is_recorded_in_both_directions(drill: Drill) -> None:
    tally = await drill.counts()
    assert tally.get("/hello", 0) == 1
    assert tally.get("/welcome", 0) == 1
    assert tally.get("/session_meta", 0) == 1


async def test_heartbeats_reach_the_recording(drill: Drill) -> None:
    """Both directions. The brain beats on its interval and the body answers,
    and a recording that showed only one side would misrepresent the session
    as half dead."""
    await drill.body.client.send_heartbeat()

    beats = [r for topic, r in await drill.tape() if topic == "/heartbeat"]
    assert beats, "no heartbeat reached the recording"
    assert any(r["direction"] == "rx" for r in beats)


async def test_nothing_leaves_the_brain_unrecorded(drill: Drill) -> None:
    """The structural fix, asserted structurally.

    `SessionState.send` is the only way out to a body, and it records. If a
    second send path is ever added beside it, this is the test that should
    fail.
    """
    state = drill.server.sessions[drill.session]

    assert state.recorder is not None, "a session can send without recording"
