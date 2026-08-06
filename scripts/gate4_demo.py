"""Gate 4 demo: the laptop body sees and speaks (checklist Gate 4).

Real camera, real synthesiser, real time. The test suite proves the logic
against stubs; this is the first time the system points a lens at the room
and says something out loud.

Run it with:

    scripts/gate4

What you should see, in order:

  0. A precondition check. The camera and microphone extras are optional
     dependencies, and if they are missing this stops and prints the exact
     command rather than half-running.

  1. The camera opens. **On the first run from a terminal that has never
     asked, macOS raises the camera permission dialog here.** Answer it. The
     grant belongs to the program that asked, so Terminal and an IDE are
     tracked separately and you may be asked again elsewhere.

     If the permission is denied, or the prompt is never answered, the body
     drops the camera from its manifest and this script stops with the named
     TCC error from step 4.1 and the settings path. It does not hang, and it
     does not carry on pretending to have a camera.

  2. The body connects. Its manifest is printed. Note the declared
     resolution: it is what the camera actually opened at, not what was
     asked for, so it may not be 1280x720.

  3. Snapshot. The brain asks, the body takes one picture, and it arrives as
     a `frame` event. The jpeg is decoded and written to
     `var/gate4-snapshot.jpg`, and the path is printed with a command to
     open it. Look at it: that is what the body saw.

  4. Speech. The brain sends a `say`. You should hear the sentence out loud.
     The result flow is visible: `running` when playback starts, `succeeded`
     when the sentence ends, with `playback_state` started and stopped
     events either side.

  5. The session is written to `var/logs/gate4-demo.mcap` and the command to
     inspect it is printed.

The microphone is deliberately not used. Gate 4 is about seeing and
speaking, and skipping it means one permission prompt rather than two.

The script fails loudly if any step does not happen. A green run means every
line above was observed, not assumed.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)

TOKEN = os.environ.get("BRAIN_AUTH_TOKEN", "gate4-demo")
PORT = int(os.environ.get("BRAIN_PORT", "8803"))

SENTENCE = "Gate four. Body one is online and can see."

SNAPSHOT_PATH = Path("var/gate4-snapshot.jpg")
SESSION_PATH = Path("var/logs/gate4-demo.mcap")

REQUIRED_EXTRAS = {
    "cv2": "opencv-python",
    "sounddevice": "sounddevice",
}


class Failed(AssertionError):
    pass


_step = 0


def step(text: str) -> None:
    global _step
    _step += 1
    print(f"\n{BOLD}[{_step}] {text}{OFF}", flush=True)


def ok(text: str) -> None:
    print(f"    {GREEN}OK{OFF}  {text}", flush=True)


def note(text: str) -> None:
    print(f"    {DIM}{text}{OFF}", flush=True)


def require(condition: bool, text: str) -> None:
    if not condition:
        raise Failed(text)
    ok(text)


async def until(condition, *, what: str, timeout_s: float = 30.0) -> None:
    waited = 0.0
    while waited < timeout_s:
        if condition():
            return
        await asyncio.sleep(0.01)
        waited += 0.01
    raise Failed(f"timed out after {timeout_s:.0f}s waiting for {what}")


def check_preconditions() -> None:
    """Refuse to half-run.

    The capture dependencies are an optional extra precisely so CI does not
    carry them. On a machine that never installed them, failing here with the
    command to fix it beats failing three steps later inside a device driver.
    """
    missing = [
        package
        for module, package in REQUIRED_EXTRAS.items()
        if importlib.util.find_spec(module) is None
    ]

    if not missing:
        ok(f"capture dependencies present: {', '.join(REQUIRED_EXTRAS.values())}")
        return

    print(
        f"\n{RED}{BOLD}MISSING DEPENDENCIES{OFF}  {', '.join(missing)}\n\n"
        f"  The laptop body's capture dependencies are an optional extra, so that\n"
        f"  CI and the test suite need neither a camera nor a microphone.\n\n"
        f"  Install them with:\n\n"
        f"      {BOLD}uv sync --extra laptop{OFF}\n\n"
        f"  then run {BOLD}scripts/gate4{OFF} again.\n",
        flush=True,
    )
    sys.exit(1)


def denied_camera(reason: str) -> str:
    return (
        f"\n{RED}{BOLD}NO CAMERA{OFF}\n\n"
        f"  The body started, connected, and did not declare a camera, so there is\n"
        f"  nothing to take a picture with. What the body reported:\n\n"
        f"      {DIM}{reason}{OFF}\n\n"
        f"  On macOS the usual cause is the camera permission. The grant belongs to\n"
        f"  the program that asked, so grant it to the terminal or IDE you ran this\n"
        f"  from:\n\n"
        f"      {BOLD}System Settings > Privacy & Security > Camera{OFF}\n\n"
        f"  If no dialog ever appeared, the prompt may have been raised somewhere\n"
        f"  you could not see it. Run this again from a foreground Terminal.\n\n"
        f"  See docs/runbook-laptop-body.md\n"
    )


async def main(
    camera: Any = None,
    microphone: Any = None,
    speaker: Any = None,
) -> int:
    """Run the gate.

    The three device sources are parameters so the orchestration can be
    exercised against stubs before anyone points a real camera at anything.
    Left unset, every one of them is the real thing, which is what
    `scripts/gate4` does.
    """
    from bodies.audio import MicrophoneUnavailableError
    from bodies.camera import OpenCVCamera
    from bodies.client import BodyConfig
    from bodies.laptop import CAMERA_ID, SPEAKER_ID, LaptopBody
    from bodies.speech import MacSaySpeaker
    from brain.config import ServerConfig
    from brain.recorder import FlightRecorder
    from brain.server import BrainServer
    from wire import CommandEnvelope, CommandPayload, without_none
    from wire.stamp import new_id, now

    class NotInThisGate:
        """Stands in for the microphone, which Gate 4 does not use.

        Declining rather than stubbing: a stub would put a microphone in the
        manifest that nothing here can use, and the manifest is supposed to
        describe what the body can actually do.
        """

        def open(self):
            raise MicrophoneUnavailableError("microphone is not part of Gate 4")

        def read(self, seconds: float):
            raise MicrophoneUnavailableError("microphone is not part of Gate 4")

        def close(self) -> None:
            return

    events: list[Any] = []
    results: dict[str, list[Any]] = {}

    async def collect(state: Any, reception: Any) -> None:
        message = reception.message
        events.append(message)
        if message.type == "command_result":
            results.setdefault(message.span_id, []).append(message)
        if message.type == "event" and message.payload.event == "playback_state":
            data = message.payload.data
            note(f"playback_state: {data['state']}  {data.get('reason', '')}".rstrip())

    config = ServerConfig(
        host="127.0.0.1",
        port=PORT,
        auth_token=TOKEN,
        heartbeat_interval_ms=500,
        heartbeat_lease_ms=1500,
    )

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    recorder = FlightRecorder(SESSION_PATH)
    recorder.open()

    async with BrainServer(config, on_message=collect, recorder=recorder) as server:
        step("Opening the camera")
        note("first run from this terminal: answer the macOS permission dialog")

        body = LaptopBody(
            BodyConfig(url=f"ws://127.0.0.1:{server.port}", auth_token=TOKEN),
            camera=camera if camera is not None else OpenCVCamera(),
            microphone=microphone if microphone is not None else NotInThisGate(),
            speaker=speaker if speaker is not None else MacSaySpeaker(),
        )

        if not body.has_camera:
            print(denied_camera("the camera did not open; see the log line above"), flush=True)
            recorder.close()
            return 1

        ok("camera opened")

        step("Body connects")
        welcome = await body.client.connect()
        session = welcome.payload.session
        await body.client.announce_boot_state()
        loops = asyncio.create_task(body.client.run_loops())

        record = server.registry.get(session)
        require(record is not None, f"handshake completed with {record.body_id!r}")

        declared = {c.id: c.capability_class for c in record.manifest.capabilities}
        note(f"manifest: {declared}")
        require(CAMERA_ID in declared, "the body declared a camera")
        require(SPEAKER_ID in declared, "the body declared a speaker")

        camera_capability = next(c for c in record.manifest.capabilities if c.id == CAMERA_ID)
        resolution = (camera_capability.attributes or {}).get("resolutions", ["?"])[0]
        note(f"declared resolution: {resolution}  (what the device actually opened at)")

        async def send(capability: str, action: str, span_id: str, **params: Any) -> None:
            state = server.sessions[session]
            message = CommandEnvelope(
                **without_none(
                    type="command",
                    id=new_id(),
                    session=session,
                    seq=state.record.outbound.take(),
                    ts=now(),
                    trace_id=f"trc_{span_id}",
                    span_id=span_id,
                    payload=CommandPayload(
                        capability=capability, action=action, params=params, ttl_ms=15000
                    ),
                )
            )
            server.registry.open_span(session, message)
            await state.send(message)

        step("Snapshot")
        await send(CAMERA_ID, "snapshot", "spn_snap")
        await until(lambda: "spn_snap" in results, what="the snapshot result")
        require(
            results["spn_snap"][-1].payload.status == "succeeded",
            "the body took a picture",
        )

        frames = [m for m in events if m.type == "event" and m.payload.event == "frame"]
        require(len(frames) == 1, "one frame event arrived")

        data = frames[0].payload.data
        SNAPSHOT_PATH.write_bytes(base64.b64decode(data["b64"]))
        ok(f"{data['width']}x{data['height']} jpeg, {SNAPSHOT_PATH.stat().st_size} bytes")

        print(
            f"\n    {BOLD}{YELLOW}Look at what the body saw:{OFF}\n"
            f"        {BOLD}open {SNAPSHOT_PATH}{OFF}\n",
            flush=True,
        )

        step("Speech")
        note(f'saying: "{SENTENCE}"')
        await send(SPEAKER_ID, "say", "spn_say", text=SENTENCE)

        await until(
            lambda: any(r.payload.status == "running" for r in results.get("spn_say", [])),
            what="playback to start",
        )
        ok("running: playback started")

        await until(
            lambda: any(r.payload.status == "succeeded" for r in results.get("spn_say", [])),
            what="the sentence to finish",
        )
        ok("succeeded: the sentence finished")

        playback = [m for m in events if m.type == "event" and m.payload.event == "playback_state"]
        states = [m.payload.data["state"] for m in playback]
        require(states == ["started", "stopped"], f"playback_state events: {states}")
        require(
            playback[1].payload.data["reason"] == "completed",
            "playback completed rather than being interrupted",
        )

        loops.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await loops
        body.close_camera()
        with contextlib.suppress(Exception):
            body.speaker.close()
        await body.client.close()

    stats = recorder.close()

    print(
        f"\n{GREEN}{BOLD}GATE 4 PASSED{OFF}  {stats.messages} messages recorded\n\n"
        f"    Snapshot:  {BOLD}open {SNAPSHOT_PATH}{OFF}\n"
        f"    Session:   {BOLD}mcap info {SESSION_PATH}{OFF}\n",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    print(f"\n{BOLD}[0] Preconditions{OFF}", flush=True)
    check_preconditions()

    try:
        sys.exit(asyncio.run(main()))
    except Failed as failure:
        print(f"\n{RED}{BOLD}GATE 4 FAILED{OFF}  {failure}\n", flush=True)
        sys.exit(1)
