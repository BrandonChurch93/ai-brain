"""Speech output, behind an interface the tests can replace.

macOS `say` is the v1 implementation. Kokoro replaces it in checklist step
6.2, and the interface is what makes that a swap rather than a rewrite.

Two things here exist for step 6.3 rather than for this step. Playback can be
interrupted part way, which is the primitive barge-in is built on. And
starting and stopping are observable as events, which is how a voice loop
gates the microphone while the speaker is talking, so the body does not
transcribe itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("bodies.speech")

#: How the manifest describes where synthesis happens (SPEC section 7.2).
TTS_LOCAL = "local"
TTS_NONE = "none"

#: The stub speaks at a fixed, unhurried pace so a test can reason about how
#: much of a sentence had been said when something interrupted it.
STUB_CHARS_PER_SECOND = 20.0


class SpeechError(RuntimeError):
    """Speech output could not be used."""


class SpeechUnavailableError(SpeechError):
    """No usable synthesiser on this machine."""


@dataclass(frozen=True, slots=True)
class SpeechFormat:
    """What the speaker can do, for the manifest."""

    tts: str = TTS_LOCAL
    voice: str | None = None

    def as_attributes(self) -> dict[str, object]:
        attributes: dict[str, object] = {"tts": self.tts}
        if self.voice is not None:
            attributes["voice"] = self.voice
        return attributes


class Speaker(Protocol):
    """Something that can say a sentence and be told to stop."""

    def open(self) -> SpeechFormat: ...

    async def start(self, text: str) -> None: ...

    async def wait(self) -> bool:
        """Block until playback ends. True if it finished, False if stopped."""
        ...

    async def stop(self) -> None: ...

    @property
    def speaking(self) -> bool: ...

    def close(self) -> None: ...


class StubSpeaker:
    """A speaker that says nothing audibly and reports everything faithfully.

    Playback does not finish on its own: a test calls `finish` when it wants
    the sentence to end. Real speech takes real seconds, and a stub that
    finished after a sleep would put wall-clock timing back into tests that
    were built to avoid it.
    """

    __slots__ = ("_done", "_format", "_interrupted", "_speaking", "said", "stopped")

    def __init__(self, speech_format: SpeechFormat | None = None) -> None:
        self._format = speech_format or SpeechFormat(tts=TTS_LOCAL, voice="stub")
        self._speaking = False
        self._interrupted = False
        self._done: asyncio.Event | None = None
        self.said: list[str] = []
        self.stopped = 0

    def open(self) -> SpeechFormat:
        return self._format

    async def start(self, text: str) -> None:
        self.said.append(text)
        self._speaking = True
        self._interrupted = False
        self._done = asyncio.Event()

    async def wait(self) -> bool:
        if self._done is None:
            return True
        await self._done.wait()
        self._speaking = False
        return not self._interrupted

    async def stop(self) -> None:
        if not self._speaking:
            return
        self.stopped += 1
        self._interrupted = True
        self._speaking = False
        if self._done is not None:
            self._done.set()

    def finish(self) -> None:
        """Let the sentence run to its end. For tests to call."""
        self._interrupted = False
        self._speaking = False
        if self._done is not None:
            self._done.set()

    @property
    def speaking(self) -> bool:
        return self._speaking

    def close(self) -> None:
        self._speaking = False


class MacSaySpeaker:
    """macOS `say`, as a subprocess.

    A subprocess rather than a library because it is already there, needs no
    permission, and can be killed. That last part is the point: `stop` has to
    actually cut a sentence off mid-word for barge-in to mean anything, and
    killing a process does that reliably where asking a library politely may
    not.
    """

    __slots__ = ("_process", "_rate", "_stopped", "_voice")

    def __init__(self, voice: str | None = None, rate: int | None = None) -> None:
        self._voice = voice
        self._rate = rate
        self._process: asyncio.subprocess.Process | None = None
        self._stopped = False

    def open(self) -> SpeechFormat:
        if shutil.which("say") is None:
            raise SpeechUnavailableError(
                "the macOS `say` command is not on PATH. This body's speaker is "
                "macOS-only in v1; Kokoro replaces it in checklist step 6.2"
            )
        return SpeechFormat(tts=TTS_LOCAL, voice=self._voice)

    async def start(self, text: str) -> None:
        argv = ["say"]
        if self._voice:
            argv += ["-v", self._voice]
        if self._rate:
            argv += ["-r", str(self._rate)]
        argv.append(text)

        self._stopped = False
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SpeechUnavailableError(f"could not run `say`: {exc}") from exc

    async def wait(self) -> bool:
        if self._process is None:
            return True

        code = await self._process.wait()
        process, self._process = self._process, None

        if self._stopped:
            return False

        if code != 0:
            log.warning("`say` exited with %s", code)
            return False

        del process
        return True

    async def stop(self) -> None:
        if self._process is None:
            return

        self._stopped = True
        # Already gone is the same outcome as just stopped.
        with contextlib.suppress(ProcessLookupError):
            self._process.terminate()

    @property
    def speaking(self) -> bool:
        return self._process is not None

    def close(self) -> None:
        if self._process is not None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
            self._process = None
