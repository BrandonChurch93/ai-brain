"""The adapters the conformance suite runs against.

One entry per body. Adding a body here subjects it to every mechanically
checkable line of the SPEC section 10 conformance checklist, which is the
point: the checklist should cost nothing to apply to the next body.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from bodies.camera import StubCamera
from bodies.client import BodyConfig
from bodies.laptop import LaptopBody, laptop_manifest
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
    #: Whether the body reports anything on a timer. A camera reports when
    #: asked, so the telemetry checks do not apply to it.
    emits_telemetry: bool = True

    def make(self, config: BodyConfig, *, clock: Clock = SYSTEM_CLOCK, **kwargs: Any) -> Adapter:
        return self.build(config, clock=clock, **kwargs)


def _stubbed_laptop(config: BodyConfig, **kwargs: Any) -> Adapter:
    """The laptop body with a stub capture source.

    The adapter under test is the real one. Only the hardware behind it is
    replaced, so CI exercises every line of the body without a camera or a
    permission prompt nobody is there to answer.
    """
    return LaptopBody(config, camera=StubCamera(), **kwargs)


ADAPTERS: list[AdapterCase] = [
    AdapterCase(name="mock", manifest=mock_manifest, build=MockBody),
    AdapterCase(
        name="laptop",
        manifest=laptop_manifest,
        build=_stubbed_laptop,
        emits_telemetry=False,
    ),
]

#: Convenience for `pytest.mark.parametrize` ids.
ADAPTER_IDS = [case.name for case in ADAPTERS]
