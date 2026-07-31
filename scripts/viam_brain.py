"""The brain half of the Viam test (checklist step 4.4).

Run against a git worktree of an older commit. This script is new; every
library it imports is whatever `PYTHONPATH` points at, which is the whole
point: an old brain, driven into talking to a body that did not exist when
it was written.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from brain.config import ServerConfig
from brain.recorder import FlightRecorder
from brain.server import BrainServer
from wire import CommandEnvelope, CommandPayload, without_none
from wire.stamp import new_id, now

TOKEN = os.environ.get("BRAIN_AUTH_TOKEN", "viam-test")
PORT = int(os.environ.get("BRAIN_PORT", "8802"))

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


class Failed(AssertionError):
    pass


def ok(text: str) -> None:
    print(f"    {GREEN}OK{OFF}  {text}", flush=True)


def note(text: str) -> None:
    print(f"    {DIM}{text}{OFF}", flush=True)


def step(text: str) -> None:
    print(f"\n{BOLD}{text}{OFF}", flush=True)


def require(condition: bool, text: str) -> None:
    if not condition:
        raise Failed(text)
    ok(text)


async def until(condition, *, what: str, timeout_s: float = 20.0) -> None:
    waited = 0.0
    while waited < timeout_s:
        if condition():
            return
        await asyncio.sleep(0.01)
        waited += 0.01
    raise Failed(f"timed out after {timeout_s:.0f}s waiting for {what}")


def command(session: str, capability: str, action: str, span_id: str, **params: Any):
    return CommandEnvelope(
        **without_none(
            type="command",
            id=new_id(),
            session=session,
            seq=0,
            ts=now(),
            trace_id=f"trc_{span_id}",
            span_id=span_id,
            payload=CommandPayload(
                capability=capability, action=action, params=params, ttl_ms=10000
            ),
        )
    )


async def main() -> int:
    events: list[Any] = []
    results: dict[str, list[Any]] = {}

    async def collect(state: Any, reception: Any) -> None:
        message = reception.message
        events.append(message)
        if message.type == "command_result":
            results.setdefault(message.span_id, []).append(message)
        if message.type == "event":
            note(f"body -> {message.payload.capability}/{message.payload.event}")

    config = ServerConfig(
        host="127.0.0.1",
        port=PORT,
        auth_token=TOKEN,
        heartbeat_interval_ms=500,
        heartbeat_lease_ms=1500,
    )
    recorder = FlightRecorder(config.log_dir / "viam-test.mcap")
    recorder.open()

    async with BrainServer(config, on_message=collect, recorder=recorder) as server:
        print(f"brain listening on {PORT}", flush=True)

        step("1. A body this brain has never heard of connects")
        await until(lambda: bool(server.sessions), what="a body to connect", timeout_s=30)
        session = next(iter(server.sessions))
        record = server.registry.get(session)
        require(record is not None, f"handshake completed with body {record.body_id!r}")

        declared = {c.id: c.capability_class for c in record.manifest.capabilities}
        note(f"manifest: {declared}")
        require(
            "cam0" in declared and declared["cam0"] == "camera",
            "the brain accepted a camera capability it has no code for",
        )
        require(
            "spk0" in declared and declared["spk0"] == "speaker",
            "and a speaker capability it has no code for",
        )

        async def send(message: Any) -> None:
            state = server.sessions[session]
            message.seq = state.record.outbound.take()
            server.registry.open_span(session, message)
            await state.send(message)

        step("2. Snapshot round trip")
        await send(command(session, "cam0", "snapshot", "spn_snap"))
        await until(lambda: "spn_snap" in results, what="a snapshot result")
        require(
            results["spn_snap"][-1].payload.status == "succeeded",
            "snapshot succeeded",
        )

        frames = [m for m in events if m.type == "event" and m.payload.event == "frame"]
        require(len(frames) == 1, "one frame event arrived")
        data = frames[0].payload.data
        require(
            data["format"] == "jpeg" and len(data["b64"]) > 0,
            f"the frame is a {data['width']}x{data['height']} jpeg, {len(data['b64'])} b64 chars",
        )

        step("3. Say round trip")
        await send(command(session, "spk0", "say", "spn_say", text="the thesis holds"))
        await until(
            lambda: any(r.payload.status == "running" for r in results.get("spn_say", [])),
            what="playback to start",
        )
        ok("say reported running when playback started")

        await until(
            lambda: any(r.payload.status == "succeeded" for r in results.get("spn_say", [])),
            what="the sentence to finish",
        )
        require(True, "say ended succeeded after the sentence finished")

        playback = [m for m in events if m.type == "event" and m.payload.event == "playback_state"]
        states = [m.payload.data["state"] for m in playback]
        require(states == ["started", "stopped"], f"playback_state events: {states}")

    stats = recorder.close()
    print(
        f"\n{GREEN}{BOLD}VIAM TEST PASSED{OFF}  "
        f"{stats.messages} messages recorded to {recorder.path}\n",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Failed as failure:
        print(f"\n{RED}{BOLD}VIAM TEST FAILED{OFF}  {failure}\n", flush=True)
        sys.exit(1)
