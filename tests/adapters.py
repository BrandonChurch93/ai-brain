"""The adapters the conformance suite runs against.

One entry per body. Adding a body here subjects it to every mechanically
checkable line of the SPEC section 10 conformance checklist, which is the
point: the checklist should cost nothing to apply to the next body.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from bodies.client import BodyConfig
from bodies.mock import MockBody, mock_manifest
from wire import Manifest
from wire.clock import SYSTEM_CLOCK, Clock


class Adapter(Protocol):
    """What the conformance suite needs from any body."""

    client: Any
    ledger: Any
    dispatch: Any

    async def emit_telemetry(self, elapsed_s: float) -> None: ...


@dataclass(frozen=True)
class AdapterCase:
    """One body under test."""

    name: str
    manifest: Callable[[], Manifest]
    build: Callable[..., Adapter]
    #: Actions the adapter is expected to handle, as (capability, action).
    #: Derived from the manifest rather than listed, so it cannot drift.

    def make(self, config: BodyConfig, *, clock: Clock = SYSTEM_CLOCK, **kwargs: Any) -> Adapter:
        return self.build(config, clock=clock, **kwargs)


ADAPTERS: list[AdapterCase] = [
    AdapterCase(name="mock", manifest=mock_manifest, build=MockBody),
]

#: Convenience for `pytest.mark.parametrize` ids.
ADAPTER_IDS = [case.name for case in ADAPTERS]
