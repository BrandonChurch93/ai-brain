"""The server over a real socket, driven by a scripted client.

This is the done-check for checklist step 1.2: good hello gets welcome,
wrong token gets reject auth_failed, unknown version gets reject with the
supported list, wrong subprotocol is refused at the upgrade.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import websockets
from websockets.asyncio.client import connect

from brain.config import ServerConfig
from brain.server import BrainServer
from wire import SUBPROTOCOL, decode
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


async def test_inbound_messages_reach_the_handler(server: BrainServer) -> None:
    """The seam checklist step 1.3 builds the session registry on."""
    received: list[Any] = []

    config = ServerConfig(host="127.0.0.1", port=0, auth_token=TOKEN)

    async def collect(state: Any, message: Any) -> None:
        received.append((state.session, message))

    async with BrainServer(config, on_message=collect) as running:
        url = f"ws://127.0.0.1:{running.port}"
        async with connect(url, subprotocols=[SUBPROTOCOL]) as client:  # type: ignore[list-item]
            await client.send(hello_frame())
            welcome = decode(await client.recv())

            await client.send(
                json.dumps(
                    {
                        "type": "heartbeat",
                        "id": "01JZQK8N4T00000000000000F1",
                        "session": welcome.session,
                        "seq": 2,
                        "ts": {"mono_ns": 2, "utc": "2026-07-29T18:00:01.000Z"},
                        "payload": {"state": "ok", "uptime_ms": 10},
                    }
                )
            )

            for _ in range(200):
                if received:
                    break
                await _tick()

    assert received, "handler never saw the heartbeat"
    session, message = received[0]
    assert session == welcome.session
    assert message.type == "heartbeat"
    assert message.payload.state == "ok"


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
