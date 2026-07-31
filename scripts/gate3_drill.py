"""Gate 3 drill: the socket dies mid-mission and the body fails safe.

This is V1 acceptance test 1, run as a demonstration rather than a unit test.
Everything here is real: two processes, a real WebSocket, real wall-clock
time, no fakes and no injected clocks. The test suite proves the logic; this
proves the thing actually does it.

Run it with:

    scripts/drill

What you should see, in order:

  1. The mock body connects and the handshake completes.
  2. It announces `safe_hold`, the state it booted into, because it declares
     actuation and SPEC section 7.1 requires that.
  3. The brain clears the safe hold, and the body reports `ok`.
  4. A drive command goes out and succeeds. The body is moving.
  5. A drive span is left open, standing in for a long-running action. Every
     mock action completes instantly, so without this nothing would still be
     in flight when the latch fires.
  6. The WebSocket is killed mid-mission.
  7. Within the lease, and with no working socket, the body latches
     `safe_hold` on its own and stops. Two warnings are expected here and are
     the point: the body cannot announce the latch or send the span result,
     and latches anyway. A body that needed the network to stop would be
     exactly backwards.
  8. The in-flight drive span ends `failed`.
  9. The body reconnects. It is still latched and says so in its first
     heartbeat: the cause went away and the latch did not.
 10. A drive command sent while latched is refused.
 11. E-stop drill: clear the hold, then E-stop from idle. `clear_safe_hold`
     alone is refused; only `estop_clear` then `clear_safe_hold` restores
     `ok`, because one message must not both release an emergency stop and
     re-enable motion.

The drill fails loudly if any step does not happen. A green run means every
line above was observed, not assumed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from typing import Any

from bodies.client import BodyConfig
from bodies.mock import MockBody
from brain.config import ServerConfig
from brain.recorder import FlightRecorder
from brain.server import BrainServer
from wire import CommandEnvelope, CommandPayload, without_none
from wire.stamp import new_id, now

TOKEN = "gate3-drill"
PORT = int(os.environ.get("BRAIN_PORT", "8801"))

# Deliberately the schema floor (SPEC section 6.2) so the drill is quick to
# watch. Production defaults are 1000/3000.
INTERVAL_MS = 100
LEASE_MS = 300

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
OFF = "\033[0m"

_step = 0


def step(text: str) -> None:
    global _step
    _step += 1
    print(f"\n{BOLD}[{_step}] {text}{OFF}", flush=True)


def observed(text: str) -> None:
    print(f"    {GREEN}OK{OFF}  {text}", flush=True)


def note(text: str) -> None:
    print(f"    {DIM}{text}{OFF}", flush=True)


class DrillFailed(AssertionError):
    pass


def require(condition: bool, text: str) -> None:
    if not condition:
        raise DrillFailed(text)
    observed(text)


async def until(condition, *, what: str, timeout_s: float = 10.0) -> None:
    waited = 0.0
    while waited < timeout_s:
        if condition():
            return
        await asyncio.sleep(0.01)
        waited += 0.01
    raise DrillFailed(f"timed out after {timeout_s:.0f}s waiting for {what}")


def command(session: str, capability: str, action: str, span_id: str, **params: Any):
    return CommandEnvelope(
        **without_none(
            type="command",
            id=new_id(),
            session=session,
            seq=0,  # replaced by the sender
            ts=now(),
            trace_id="trc_gate3",
            span_id=span_id,
            payload=CommandPayload(
                capability=capability,
                action=action,
                params=params,
                ttl_ms=5000,
            ),
        )
    )


async def send_command(server: BrainServer, session: str, message: CommandEnvelope) -> None:
    state = server.sessions[session]
    message.seq = state.record.outbound.take()
    server.registry.open_span(session, message)
    await state.send(message)


async def main() -> int:
    events: list[Any] = []
    results: dict[str, Any] = {}

    async def collect(state: Any, reception: Any) -> None:
        message = reception.message
        events.append(message)
        if message.type == "command_result":
            results[message.span_id] = message
        if message.type == "event" and message.payload.event == "state":
            data = message.payload.data
            note(f"body -> {data['state']}  ({data.get('cause')})")

    def states() -> list[str]:
        return [
            m.payload.data["state"]
            for m in events
            if m.type == "event" and m.payload.event == "state"
        ]

    config = ServerConfig(
        host="127.0.0.1",
        port=PORT,
        auth_token=TOKEN,
        heartbeat_interval_ms=INTERVAL_MS,
        heartbeat_lease_ms=LEASE_MS,
    )

    recorder = FlightRecorder(config.log_dir / "gate3-drill.mcap")
    recorder.open()

    async with BrainServer(config, on_message=collect, recorder=recorder) as server:
        body = MockBody(
            BodyConfig(url=f"ws://127.0.0.1:{server.port}", auth_token=TOKEN),
        )

        step("Body connects and completes the handshake")
        welcome = await body.client.connect()
        session = welcome.payload.session
        await body.client.announce_boot_state()
        loops = asyncio.create_task(body.client.run_loops())
        watching = asyncio.create_task(body.watch_brain_lease())

        require(body.client.connected, f"session open, protocol {body.client.protocol_version}")
        require(
            body.client.heartbeat_lease_ms == LEASE_MS,
            f"brain named a {LEASE_MS}ms lease on a {INTERVAL_MS}ms interval",
        )

        step("Body announces the state it booted into")
        await until(lambda: "safe_hold" in states(), what="the boot state event")
        require(body.state == "safe_hold", "booted into safe_hold, as an actuating body must")

        step("Brain clears the safe hold")
        await send_command(server, session, command(session, "sys", "clear_safe_hold", "spn_clear"))
        await until(lambda: body.state == "ok", what="the body to reach ok")
        require(body.state == "ok", "body is ok and may move")

        step("Mission command in flight")
        await send_command(
            server, session, command(session, "drive0", "set_velocity", "spn_drive", linear_mps=0.2)
        )
        await until(lambda: "spn_drive" in results, what="the drive result")
        require(results["spn_drive"].payload.status == "succeeded", "drive command succeeded")
        require(body.pose.linear_mps > 0, f"body is moving at {body.pose.linear_mps} m/s")

        step("A span is left in flight")
        # Every mock action completes instantly, so nothing would still be
        # running when the latch fires. This admits and starts a span without
        # finishing it, which is exactly what a long-running action would
        # leave behind, and is what SPEC section 8.1 means by "in-flight".
        held = command(session, "drive0", "set_velocity", "spn_inflight", linear_mps=0.3)
        entry = body.ledger.admit(held, body.client.stamp())
        body.ledger.start(entry)
        require(entry.terminal is None, "a drive span is open and unfinished")

        step("KILL: the WebSocket dies mid-mission")
        await server.sessions[session].connection.close()
        note(f"socket closed from the brain side; the lease is {LEASE_MS}ms")

        step("Body latches safe_hold on its own, within the lease")
        await until(lambda: body.state == "safe_hold", what="the body to latch", timeout_s=5.0)
        require(body.state == "safe_hold", "body latched safe_hold without being told")
        require(body.pose.linear_mps == 0.0, "body stopped")

        lease = body.client.brain_lease
        require(lease is not None and lease.expired, "the brain lease had expired")

        step("The in-flight actuation span fails")
        require(entry.terminal == "failed", "in-flight drive span ended failed")
        require(
            body.safety.state.value == "safe_hold",
            "and the body is holding, not merely reporting",
        )

        step("Reconnect: the latch survives a new session")
        loops.cancel()
        watching.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await loops
        await body.client.close()

        await body.client.connect()
        session = body.client.session
        loops = asyncio.create_task(body.client.run_loops())
        require(body.state == "safe_hold", "still latched after reconnecting")

        events.clear()
        await body.client.send_heartbeat()
        await until(
            lambda: any(m.type == "heartbeat" for m in events),
            what="the first heartbeat of the new session",
        )
        beat = next(m for m in events if m.type == "heartbeat")
        require(beat.payload.state == "safe_hold", "first heartbeat reports the latch")

        step("A drive command while latched is refused")
        await send_command(
            server,
            session,
            command(session, "drive0", "set_velocity", "spn_refused", linear_mps=0.2),
        )
        await until(lambda: "spn_refused" in results, what="the refusal")
        require(
            results["spn_refused"].payload.status == "rejected",
            "actuation refused while latched",
        )

        step("E-stop drill")
        session = body.client.session
        loops = asyncio.create_task(body.client.run_loops())
        await send_command(
            server, session, command(session, "sys", "clear_safe_hold", "spn_clear2")
        )
        await until(lambda: body.state == "ok", what="the body to reach ok")
        require(body.state == "ok", "cleared back to ok")

        await body.estop("operator pressed the button")
        require(body.state == "estopped", "E-stop latched from idle")

        await send_command(server, session, command(session, "sys", "clear_safe_hold", "spn_bad"))
        await until(lambda: "spn_bad" in results, what="the refused clear")
        require(
            results["spn_bad"].payload.status == "rejected",
            "clear_safe_hold alone is refused while estopped",
        )

        await body.clear_estop("obstacle removed", "brandon")
        require(body.state == "safe_hold", "estop_clear lands in safe_hold, not in motion")

        await send_command(server, session, command(session, "sys", "clear_safe_hold", "spn_good"))
        await until(lambda: body.state == "ok", what="the final clear")
        require(body.state == "ok", "estop_clear plus clear_safe_hold restored ok")

        loops.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await loops
        await body.client.close()

    stats = recorder.close()
    print(
        f"\n{GREEN}{BOLD}DRILL PASSED{OFF}  {stats.messages} messages recorded to {recorder.path}",
        flush=True,
    )
    print(f"{DIM}Inspect it with: mcap info {recorder.path}{OFF}\n", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except DrillFailed as failure:
        print(f"\n{RED}{BOLD}DRILL FAILED{OFF}  {failure}\n", flush=True)
        sys.exit(1)
