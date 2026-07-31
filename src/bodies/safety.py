"""Latched body state (SPEC section 8, ADR-0006).

The rule this file exists to enforce: `safe_hold` and `estopped` never clear
themselves. Not on reconnect, not on timeout, not because the thing that
caused them went away. Silent resume is prohibited, and every path back to
motion is an explicit instruction from someone.

Which is why this is owned by the adapter and not by `BodyClient`. A client
is session-scoped and a reconnect builds a new one; a body that latched
`safe_hold` and then reconnected into `ok` would have cleared itself by
losing its memory, which is the exact failure SPEC section 8.2 forbids.

Motion is permissioned (section 8.4): stopped is the default, and actuating
requires a non-latched state, a validator-passed command, and an unexpired
TTL. Absence of any one means stillness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger("bodies.safety")

#: Code a body puts on spans it fails because it latched (SPEC section 8.1).
LATCHED_SAFE_STATE = "latched_safe_state"


class State(StrEnum):
    """The three states a body reports (SPEC section 6.4)."""

    OK = "ok"
    SAFE_HOLD = "safe_hold"
    ESTOPPED = "estopped"


#: Latched states, in order of severity. E-stop outranks safe_hold: a body
#: that is both must report the worse of the two, and clearing the milder one
#: must not release the stronger.
SEVERITY = {State.OK: 0, State.SAFE_HOLD: 1, State.ESTOPPED: 2}

LATCHED = frozenset({State.SAFE_HOLD, State.ESTOPPED})


@dataclass(frozen=True, slots=True)
class Transition:
    """What a request to change state actually did."""

    state: State
    changed: bool
    cause: str
    refused: str | None = None

    @property
    def ok(self) -> bool:
        return self.refused is None


class SafetyState:
    """One body's latched state, outliving any single session."""

    __slots__ = ("_cause", "_state")

    def __init__(self, boot_state: str | State = State.SAFE_HOLD) -> None:
        self._state = State(boot_state)
        self._cause = "boot"

    @property
    def state(self) -> State:
        return self._state

    @property
    def cause(self) -> str:
        """Why the body is in this state. Goes into the state event."""
        return self._cause

    @property
    def latched(self) -> bool:
        return self._state in LATCHED

    @property
    def may_actuate(self) -> bool:
        """SPEC section 8.4. Nothing moves from a latched state."""
        return self._state is State.OK

    # Entering a worse state

    def enter_safe_hold(self, cause: str) -> Transition:
        """Latch `safe_hold`, unless already in something worse.

        A lease miss during an E-stop must not quietly downgrade the body to
        the milder state, or clearing the safe hold would release a stop
        nobody cleared.
        """
        if self._state is State.ESTOPPED:
            return Transition(
                state=self._state,
                changed=False,
                cause=self._cause,
                refused="already estopped; safe_hold would be a downgrade",
            )

        return self._move(State.SAFE_HOLD, cause)

    def estop(self, reason: str) -> Transition:
        """Latch `estopped`. Always allowed, from any state (section 8.3)."""
        return self._move(State.ESTOPPED, reason)

    # Coming back, only ever explicitly

    def clear_safe_hold(self, cause: str = "clear_safe_hold") -> Transition:
        """Release `safe_hold` (section 8.2). Refuses while estopped."""
        if self._state is State.ESTOPPED:
            return Transition(
                state=self._state,
                changed=False,
                cause=self._cause,
                refused="estopped; clear the E-stop first",
            )

        if self._state is State.OK:
            return Transition(state=State.OK, changed=False, cause=self._cause)

        return self._move(State.OK, cause)

    def clear_estop(self, cause: str = "estop_clear") -> Transition:
        """Release `estopped` into `safe_hold`, not into `ok`.

        SPEC section 8.2 says each latch clears only through its own path,
        and section 8.4 says motion is permissioned. Landing in `ok` would
        make one message both release an emergency stop and re-enable
        motion, so the operator would be authorising more than they said.
        The body still needs an explicit `clear_safe_hold` to move.
        """
        if self._state is not State.ESTOPPED:
            return Transition(
                state=self._state,
                changed=False,
                cause=self._cause,
                refused="not estopped",
            )

        return self._move(State.SAFE_HOLD, cause)

    def _move(self, target: State, cause: str) -> Transition:
        if self._state is target:
            return Transition(state=target, changed=False, cause=self._cause)

        previous = self._state
        self._state = target
        self._cause = cause

        log.info(
            "state %s -> %s (%s)",
            previous.value,
            target.value,
            cause,
        )
        return Transition(state=target, changed=True, cause=cause)
