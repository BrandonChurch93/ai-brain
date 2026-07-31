"""Shared test helpers.

Timing helpers live here rather than being copied between test modules, so
there is one definition of what "advance the clock" means in this suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from wire import Manifest
from wire.clock import ManualClock


class LoopFinished(Exception):
    """Ends a loop that otherwise runs forever by design."""


def ticker(
    clock: ManualClock, ticks: int, *, park: bool = False
) -> Callable[[float], Coroutine[Any, Any, None]]:
    """A sleeper that advances the fake clock instead of waiting.

    Allows exactly `ticks` iterations, then either stops the loop or parks.
    Deterministic in both directions: the same number of iterations and the
    same elapsed time on every run, on any machine.

    `park=True` for loops owned by something else, such as the server's
    per-session beat task. Raising there would kill the task, and the session
    teardown that awaits it would then re-raise into unrelated code. Parking
    leaves the task alive and cancellable, which is how it ends in
    production.
    """
    remaining = ticks

    async def sleep(seconds: float) -> None:
        nonlocal remaining
        if remaining <= 0:
            if park:
                await asyncio.Event().wait()  # until cancelled
                return
            raise LoopFinished
        remaining -= 1
        clock.advance(seconds=seconds)
        await asyncio.sleep(0)  # yield to the event loop, consume no real time

    return sleep


def mock_manifest(body_id: str = "mock-01") -> Manifest:
    return Manifest.model_validate(
        {
            "body_id": body_id,
            "hardware_class": "virtual",
            "boot_state": "safe_hold",
            "adapter": {"name": "mock-adapter", "version": "0.1.0"},
            "capabilities": [
                {"id": "sys", "class": "system", "actions": ["ping", "clear_safe_hold"]},
                {
                    "id": "drive0",
                    "class": "differential_drive",
                    "actions": ["set_velocity", "stop"],
                },
            ],
        }
    )
