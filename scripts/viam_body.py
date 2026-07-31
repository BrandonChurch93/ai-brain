"""The body half of the Viam test (checklist step 4.4).

The current laptop body, unmodified, pointed at whatever brain is listening.

Camera and microphone use stub sources. The thesis under test is protocol
compatibility, not whether a webcam is plugged in, and a real capture here
would raise a macOS permission dialog in a context with nobody to answer it.
The speaker is real: `say` needs no permission, so the sentence is audible.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from bodies.audio import StubMicrophone
from bodies.camera import StubCamera
from bodies.client import BodyConfig
from bodies.laptop import LaptopBody
from bodies.speech import MacSaySpeaker

logging.basicConfig(level="INFO", format="BODY %(levelname)s %(message)s")


async def main() -> None:
    body = LaptopBody(
        BodyConfig(
            url=os.environ.get("BRAIN_URL", "ws://127.0.0.1:8802"),
            auth_token=os.environ.get("BRAIN_AUTH_TOKEN", "viam-test"),
        ),
        camera=StubCamera(),
        microphone=StubMicrophone(),
        speaker=MacSaySpeaker(rate=300),
    )
    await body.run()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, Exception):
        asyncio.run(main())
