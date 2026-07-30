"""The server over a real socket, driven by a scripted client.

This is the done-check for checklist step 1.2: good hello gets welcome,
wrong token gets reject auth_failed, unknown version gets reject with the
supported list, wrong subprotocol is refused at the upgrade.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import websockets
from mcap.reader import make_reader
from websockets.asyncio.client import connect

from brain.config import ServerConfig
from brain.recorder import FlightRecorder
from brain.registry import BODY_LOST, SeqStatus
from brain.server import BrainServer
from wire import SUBPROTOCOL, decode, decode_object
from wire.schema import protocol_version

TOKEN = "test-token"


@pytest.fixture
async def server() -> AsyncIterator[BrainServer]:
    config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)
    async with BrainServer(config) as running:
        yield running


def hello_frame(
    *,
    token: str = TOKEN,
    versions: list[str] | None = None,
    body_id: str = "mock-01",
) -> str:
    return json.dumps(
        {
            "type": "hello",
            "id": "01JZQK8N4T00000000000000A1",
            "seq": 1,
            "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
            "payload": {
                "protocol_versions": versions if versions is not None else [protocol_version()],
                "auth_token": token,
                "manifest": {
                    "body_id": body_id,
                    "hardware_class": "virtual",
                    "boot_state": "safe_hold",
                    "adapter": {"name": "mock-adapter", "version": "0.1.0"},
                    "capabilities": [{"id": "sys", "class": "system", "actions": ["ping"]}],
                },
            },
        }
    )


async def exchange(server: BrainServer, frame: str) -> Any:
    """Connect properly, send one frame, return the decoded reply."""
    url = f"ws://127.0.0.1:{server.port}"
    async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
        await client.send(frame)
        return decode(await client.recv())


async def test_good_hello_gets_welcome(server: BrainServer) -> None:
    reply = await exchange(server, hello_frame())

    assert reply.type == "welcome"
    assert reply.payload.protocol_version == protocol_version()
    assert reply.payload.server.name == "brain"
    assert reply.payload.heartbeat.lease_ms > reply.payload.heartbeat.interval_ms
    assert reply.session == reply.payload.session


async def test_wrong_token_gets_reject_auth_failed(server: BrainServer) -> None:
    reply = await exchange(server, hello_frame(token="not-the-token"))

    assert reply.type == "reject"
    assert reply.payload.code == "auth_failed"


async def test_unknown_version_gets_reject_with_supported_list(server: BrainServer) -> None:
    reply = await exchange(server, hello_frame(versions=["2099-01-01"]))

    assert reply.type == "reject"
    assert reply.payload.code == "unsupported_version"
    assert reply.payload.supported == [protocol_version()]


async def test_wrong_subprotocol_is_refused_at_the_upgrade(server: BrainServer) -> None:
    """SPEC section 3.2. The connection never opens, so no reject is sent:
    the refusal is an HTTP 400 on the upgrade itself."""
    url = f"ws://127.0.0.1:{server.port}"
    with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
        async with connect(url, subprotocols=["chat"]):  # type: ignore[list-item]
            pass

    assert caught.value.response.status_code == 400


async def test_missing_subprotocol_is_refused_at_the_upgrade(server: BrainServer) -> None:
    url = f"ws://127.0.0.1:{server.port}"
    with pytest.raises(websockets.exceptions.InvalidStatus) as caught:
        async with connect(url):
            pass

    assert caught.value.response.status_code == 400


async def test_negotiated_subprotocol_is_reported_to_the_client(server: BrainServer) -> None:
    url = f"ws://127.0.0.1:{server.port}"
    async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
        assert client.subprotocol == SUBPROTOCOL


async def test_a_first_frame_that_is_not_json_is_refused(server: BrainServer) -> None:
    reply = await exchange(server, "{not json at all")

    assert reply.type == "reject"
    assert reply.payload.code == "malformed_hello"


async def test_a_first_frame_that_is_not_hello_is_refused(server: BrainServer) -> None:
    frame = json.dumps(
        {
            "type": "heartbeat",
            "id": "01JZQK8N4T00000000000000F1",
            "session": "sess_invented",
            "seq": 1,
            "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
            "payload": {"state": "ok"},
        }
    )
    reply = await exchange(server, frame)

    assert reply.type == "reject"
    assert reply.payload.code == "malformed_hello"


async def test_a_schema_invalid_hello_is_refused(server: BrainServer) -> None:
    """No manifest, so it is not a hello the contract recognises."""
    frame = json.dumps(
        {
            "type": "hello",
            "id": "01JZQK8N4T00000000000000A1",
            "seq": 1,
            "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
            "payload": {"protocol_versions": [protocol_version()], "auth_token": TOKEN},
        }
    )
    reply = await exchange(server, frame)

    assert reply.type == "reject"
    assert reply.payload.code == "malformed_hello"


async def test_the_socket_closes_after_a_reject(server: BrainServer) -> None:
    """SPEC section 6.3: reject, then close."""
    url = f"ws://127.0.0.1:{server.port}"
    async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
        await client.send(hello_frame(token="wrong"))
        assert decode(await client.recv()).type == "reject"

        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await client.recv()


async def test_an_accepted_session_is_registered_then_released(server: BrainServer) -> None:
    url = f"ws://127.0.0.1:{server.port}"
    async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
        await client.send(hello_frame())
        welcome = decode(await client.recv())

        assert welcome.session in server.sessions
        state = server.sessions[welcome.session]
        assert state.body_id == "mock-01"
        assert state.protocol_version == protocol_version()


async def test_two_bodies_get_distinct_sessions(server: BrainServer) -> None:
    url = f"ws://127.0.0.1:{server.port}"
    async with (
        connect(url, subprotocols=[SUBPROTOCOL]) as first,  # type: ignore[list-item]
        connect(url, subprotocols=[SUBPROTOCOL]) as second,  # type: ignore[list-item]
    ):
        await first.send(hello_frame(body_id="mock-01"))
        await second.send(hello_frame(body_id="mock-02"))

        one = decode(await first.recv())
        two = decode(await second.recv())

        assert one.session != two.session
        assert {one.session, two.session} <= set(server.sessions)


def heartbeat_frame(session: str, seq: int) -> str:
    return json.dumps(
        {
            "type": "heartbeat",
            "id": f"01JZQK8N4T0000000000000{seq:03d}",
            "session": session,
            "seq": seq,
            "ts": {"mono_ns": seq * 1000, "utc": "2026-07-29T18:00:01.000Z"},
            "payload": {"state": "ok", "uptime_ms": 10},
        }
    )


async def test_inbound_messages_reach_the_handler_with_a_reception() -> None:
    received: list[Any] = []

    config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)

    async def collect(state: Any, reception: Any) -> None:
        received.append((state.session, reception))

    async with BrainServer(config, on_message=collect) as running:
        url = f"ws://127.0.0.1:{running.port}"
        async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
            await client.send(hello_frame())
            welcome = decode(await client.recv())

            await client.send(heartbeat_frame(welcome.session, 2))

            for _ in range(200):
                if received:
                    break
                await _tick()

    assert received, "handler never saw the heartbeat"
    session, reception = received[0]
    assert session == welcome.session
    assert reception.message.type == "heartbeat"
    assert reception.message.payload.state == "ok"
    assert reception.seq_status is SeqStatus.IN_ORDER
    assert reception.t_received.mono_ns > 0


async def test_the_server_detects_a_seq_gap_end_to_end() -> None:
    """The body's hello was seq 1, so jumping to 5 skips three."""
    received: list[Any] = []

    config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)

    async def collect(state: Any, reception: Any) -> None:
        received.append(reception)

    async with BrainServer(config, on_message=collect) as running:
        url = f"ws://127.0.0.1:{running.port}"
        async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
            await client.send(hello_frame())
            welcome = decode(await client.recv())

            await client.send(heartbeat_frame(welcome.session, 5))

            for _ in range(200):
                if received:
                    break
                await _tick()

    assert received
    assert received[0].seq_status is SeqStatus.GAP
    assert received[0].missing == 3


async def test_a_session_leaves_the_registry_when_the_socket_closes(
    server: BrainServer,
) -> None:
    url = f"ws://127.0.0.1:{server.port}"
    async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
        await client.send(hello_frame())
        welcome = decode(await client.recv())
        assert welcome.session in server.registry

    for _ in range(200):
        if welcome.session not in server.registry:
            break
        await _tick()

    assert welcome.session not in server.registry
    assert welcome.session not in server.sessions


async def test_the_registry_holds_the_manifest_from_hello(server: BrainServer) -> None:
    url = f"ws://127.0.0.1:{server.port}"
    async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
        await client.send(hello_frame())
        welcome = decode(await client.recv())

        record = server.registry.get(welcome.session)
        assert record is not None
        assert record.manifest.body_id == "mock-01"
        assert [c.id for c in record.manifest.capabilities] == ["sys"]


async def test_outbound_seq_continues_after_welcome(server: BrainServer) -> None:
    """`welcome` was seq 1; the next thing the brain sends must be seq 2, not
    a repeat of 1."""
    url = f"ws://127.0.0.1:{server.port}"
    async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
        await client.send(hello_frame())
        welcome = decode(await client.recv())

        assert welcome.seq == 1
        record = server.registry.get(welcome.session)
        assert record is not None
        assert record.outbound.peek == 2


async def test_an_invalid_message_after_welcome_does_not_kill_the_session(
    server: BrainServer,
) -> None:
    url = f"ws://127.0.0.1:{server.port}"
    async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
        await client.send(hello_frame())
        welcome = decode(await client.recv())

        await client.send("{garbage")
        for _ in range(200):
            if welcome.session in server.sessions:
                break
            await _tick()

        assert welcome.session in server.sessions


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.005)


async def test_a_live_session_is_recorded_in_both_directions(tmp_path: Path) -> None:
    """The handshake itself must land in the log. hello arrives before the
    session exists, so it is easy to lose, and losing it would mean the file
    no longer shows the manifest the session was built on."""
    path = tmp_path / "session.mcap"

    with FlightRecorder(path) as recorder:
        config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)
        async with BrainServer(config, recorder=recorder) as running:
            url = f"ws://127.0.0.1:{running.port}"
            async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
                await client.send(hello_frame())
                welcome = decode(await client.recv())
                await client.send(heartbeat_frame(welcome.session, 2))

                for _ in range(200):
                    if recorder.message_count >= 4:
                        break
                    await _tick()

    with path.open("rb") as handle:
        records = [
            (channel.topic, json.loads(message.data))
            for _schema, channel, message in make_reader(handle).iter_messages()
        ]

    by_topic = {topic for topic, _ in records}
    assert {"/session_meta", "/hello", "/welcome", "/heartbeat"} <= by_topic

    hello = next(record for topic, record in records if topic == "/hello")
    assert hello["direction"] == "rx"
    assert hello["message"]["payload"]["manifest"]["body_id"] == "mock-01"

    sent = next(record for topic, record in records if topic == "/welcome")
    assert sent["direction"] == "tx"
    assert sent["t_received"] is None

    beat = next(record for topic, record in records if topic == "/heartbeat")
    assert beat["t_received"] is not None


async def test_a_rejected_body_is_still_recorded(tmp_path: Path) -> None:
    """A refused handshake is exactly the kind of thing you want in the log."""
    path = tmp_path / "rejected.mcap"

    with FlightRecorder(path) as recorder:
        config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)
        async with BrainServer(config, recorder=recorder) as running:
            url = f"ws://127.0.0.1:{running.port}"
            async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
                await client.send(hello_frame(token="wrong"))
                assert decode(await client.recv()).type == "reject"

    with path.open("rb") as handle:
        topics = [
            channel.topic for _schema, channel, _message in make_reader(handle).iter_messages()
        ]
    assert topics == ["/reject"]


async def test_a_recorder_failure_does_not_take_the_session_down() -> None:
    """A flight recorder that can crash the aircraft is worse than none."""

    class BrokenRecorder(FlightRecorder):
        def record(self, *args: Any, **kwargs: Any) -> None:
            raise OSError("disk went away")

        def record_session(self, *args: Any, **kwargs: Any) -> None:
            raise OSError("disk went away")

    broken = BrokenRecorder("/dev/null/never-used.mcap")
    config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)

    async with BrainServer(config, recorder=broken) as running:
        url = f"ws://127.0.0.1:{running.port}"
        async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
            await client.send(hello_frame())
            assert decode(await client.recv()).type == "welcome"


# Step 1.4 done-check: a client that goes silent.

FAST = ServerConfig(
    host="127.0.0.1",
    port=0,
    auth_token=TOKEN,
    heartbeat_interval_ms=100,
    heartbeat_lease_ms=300,
)


async def test_the_brain_sends_heartbeats_on_the_interval() -> None:
    async with BrainServer(FAST) as running:
        url = f"ws://127.0.0.1:{running.port}"
        async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
            await client.send(hello_frame())
            assert decode(await client.recv()).type == "welcome"

            beats = [decode(await client.recv()) for _ in range(3)]

    assert [beat.type for beat in beats] == ["heartbeat"] * 3
    assert {beat.payload.state for beat in beats} == {"active"}
    # welcome was seq 1, so the beats continue from 2 without repeating it.
    assert [beat.seq for beat in beats] == [2, 3, 4]


async def test_brain_heartbeats_report_degraded_when_the_brain_is() -> None:
    """SPEC section 8.5: this is the only way DEGRADED reaches a body."""
    async with BrainServer(FAST) as running:
        running.brain_state = "degraded"
        url = f"ws://127.0.0.1:{running.port}"
        async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
            await client.send(hello_frame())
            await client.recv()
            assert decode(await client.recv()).payload.state == "degraded"


async def test_a_silent_body_is_marked_lost_within_the_lease() -> None:
    """The done-check for step 1.4. The client connects, completes the
    handshake, then says nothing at all."""
    lost: list[Any] = []

    async def on_lost(state: Any, outcomes: Any) -> None:
        lost.append((state.session, outcomes))

    async with BrainServer(FAST, on_lost=on_lost) as running:
        url = f"ws://127.0.0.1:{running.port}"
        async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
            await client.send(hello_frame())
            welcome = decode(await client.recv())

            deadline = 3.0
            waited = 0.0
            while not lost and waited < deadline:
                await _tick()
                waited += 0.005

            assert lost, f"body not marked LOST within {deadline}s"
            session, _ = lost[0]
            assert session == welcome.session

            record = running.registry.get(welcome.session)
            assert record is not None
            assert record.lost


async def test_a_body_that_keeps_heartbeating_is_never_lost() -> None:
    lost: list[Any] = []

    async def on_lost(state: Any, outcomes: Any) -> None:
        lost.append(state.session)

    async with BrainServer(FAST, on_lost=on_lost) as running:
        url = f"ws://127.0.0.1:{running.port}"
        async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
            await client.send(hello_frame())
            welcome = decode(await client.recv())

            for seq in range(2, 12):
                await client.send(heartbeat_frame(welcome.session, seq))
                await asyncio.sleep(0.05)

            record = running.registry.get(welcome.session)
            assert record is not None
            assert not record.lost

    assert not lost


async def test_outstanding_spans_fail_when_the_body_is_lost() -> None:
    """SPEC section 8.1: nothing upstream should wait forever on a result
    that is never coming."""
    lost: list[Any] = []

    async def on_lost(state: Any, outcomes: Any) -> None:
        lost.append(outcomes)

    async with BrainServer(FAST, on_lost=on_lost) as running:
        url = f"ws://127.0.0.1:{running.port}"
        async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
            await client.send(hello_frame())
            welcome = decode(await client.recv())

            command = decode_object(
                {
                    "type": "command",
                    "id": "01JZQK8N4T00000000000000K1",
                    "session": welcome.session,
                    "seq": 2,
                    "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
                    "trace_id": "trc_patrol",
                    "span_id": "spn_in_flight",
                    "payload": {
                        "capability": "sys",
                        "action": "ping",
                        "params": {},
                        "ttl_ms": 5000,
                    },
                }
            )
            running.registry.open_span(welcome.session, command)  # type: ignore[arg-type]

            waited = 0.0
            while not lost and waited < 3.0:
                await _tick()
                waited += 0.005

            assert lost, "body never marked LOST"
            outcomes = lost[0]
            assert [outcome.span.span_id for outcome in outcomes] == ["spn_in_flight"]
            assert outcomes[0].status == "failed"
            assert outcomes[0].code == BODY_LOST
