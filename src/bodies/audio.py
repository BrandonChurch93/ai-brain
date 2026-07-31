"""Microphone capture, behind an interface the tests can replace.

`open` returns the format the device actually gave, not the one asked for.
Audio hardware substitutes freely: request 16 kHz mono and a device may hand
back 48 kHz stereo without complaint. The manifest is built from what comes
back here, because step 6.1 feeds this audio to Whisper and Whisper cares
about the real sample rate. A manifest declaring the requested rate would be
a lie that only shows up as bad transcription much later.

Same TCC story as the camera, and the same answer: fail fast and name the
permission (see `bodies.permissions`).
"""

from __future__ import annotations

import base64
import io
import logging
import math
import struct
import wave
from dataclasses import dataclass
from typing import Protocol

from bodies.permissions import tcc_message

log = logging.getLogger("bodies.audio")

#: What to ask for. Whisper resamples to 16 kHz mono, so asking for it means
#: no resampling when the device agrees, and honest attributes when it does
#: not (SPEC section 7.2 microphone attributes).
DEFAULT_SAMPLE_RATE_HZ = 16000
DEFAULT_CHANNELS = 1

#: Signed 16-bit little-endian PCM. The encoding name goes in the manifest
#: and on every chunk, so a consumer never has to guess at the bytes.
PCM_S16LE = "pcm_s16le"
SAMPLE_WIDTH_BYTES = 2

#: How much audio one `audio_chunk` event carries. Small enough that
#: push-to-talk feels responsive, large enough not to flood a JSON transport
#: (SPEC section 6.5: media is base64 inside JSON at modest rates in v1).
DEFAULT_CHUNK_MS = 250

DEFAULT_OPEN_TIMEOUT_S = 5.0


class MicrophoneError(RuntimeError):
    """The microphone could not be used. The message says why."""


class MicrophonePermissionError(MicrophoneError):
    """Access was refused, or a permission prompt was never answered."""


class MicrophoneUnavailableError(MicrophoneError):
    """No usable device, or the driver would not produce audio."""


class MicrophoneDependencyError(MicrophoneError):
    """The optional capture dependency is not installed."""


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """What a device actually opened at.

    Never what was requested. The two differ often enough that conflating
    them is a bug waiting for a transcription to go wrong.
    """

    sample_rate_hz: int
    channels: int
    encoding: str = PCM_S16LE

    def as_attributes(self) -> dict[str, object]:
        """The microphone attributes for the manifest (SPEC section 7.2)."""
        return {
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "encoding": self.encoding,
        }

    @property
    def frame_bytes(self) -> int:
        return SAMPLE_WIDTH_BYTES * self.channels


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """One slice of captured audio, in the format the device gave."""

    pcm: bytes
    format: AudioFormat

    @property
    def frames(self) -> int:
        return len(self.pcm) // self.format.frame_bytes

    @property
    def duration_ms(self) -> float:
        return self.frames * 1000 / self.format.sample_rate_hz

    @property
    def b64(self) -> str:
        return base64.b64encode(self.pcm).decode("ascii")

    def as_event_data(self) -> dict[str, object]:
        """The `audio_chunk` event payload.

        Carries the format on every chunk rather than only in the manifest. A
        recorded session is read back long after the manifest scrolled past,
        and PCM whose sample rate you have to go and look up is PCM you can
        get wrong.
        """
        return {
            "b64": self.b64,
            "encoding": self.format.encoding,
            "sample_rate_hz": self.format.sample_rate_hz,
            "channels": self.format.channels,
            "frames": self.frames,
            "duration_ms": round(self.duration_ms, 3),
        }

    def to_wav(self) -> bytes:
        """A playable WAV, for listening to what was captured."""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(self.format.channels)
            handle.setsampwidth(SAMPLE_WIDTH_BYTES)
            handle.setframerate(self.format.sample_rate_hz)
            handle.writeframes(self.pcm)
        return buffer.getvalue()


class AudioSource(Protocol):
    """Where audio comes from."""

    def open(self) -> AudioFormat: ...

    def read(self, seconds: float) -> AudioChunk: ...

    def close(self) -> None: ...


class StubMicrophone:
    """A microphone that is always there and always says the same thing.

    Generates a tone rather than silence, so a round trip that loses or
    truncates the audio is visible instead of looking like a quiet room. The
    same input every run, because a replayed session must reproduce its
    decisions (ADR-0005).
    """

    __slots__ = ("_format", "_opened", "reads")

    def __init__(self, audio_format: AudioFormat | None = None) -> None:
        self._format = audio_format or AudioFormat(
            sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ, channels=DEFAULT_CHANNELS
        )
        self._opened = False
        self.reads = 0

    def open(self) -> AudioFormat:
        self._opened = True
        return self._format

    def read(self, seconds: float) -> AudioChunk:
        if not self._opened:
            raise MicrophoneUnavailableError("read before open")
        self.reads += 1
        return AudioChunk(pcm=tone(self._format, seconds), format=self._format)

    def close(self) -> None:
        self._opened = False


def tone(audio_format: AudioFormat, seconds: float, hz: float = 440.0) -> bytes:
    """A 440 Hz sine, as signed 16-bit little-endian PCM."""
    frames = int(audio_format.sample_rate_hz * seconds)
    amplitude = 12000

    samples = bytearray()
    for index in range(frames):
        value = int(amplitude * math.sin(2 * math.pi * hz * index / audio_format.sample_rate_hz))
        samples += struct.pack("<h", value) * audio_format.channels
    return bytes(samples)


class SoundDeviceMicrophone:
    """A real microphone, via `sounddevice`.

    Imported lazily: only a machine with a microphone needs it, and CI has
    neither one nor anyone to approve its use.
    """

    __slots__ = ("_device", "_format", "_requested_channels", "_requested_rate", "_stream")

    def __init__(
        self,
        device: int | str | None = None,
        *,
        sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ,
        channels: int = DEFAULT_CHANNELS,
    ) -> None:
        self._device = device
        self._requested_rate = sample_rate_hz
        self._requested_channels = channels
        self._stream = None
        self._format: AudioFormat | None = None

    def open(self) -> AudioFormat:
        sounddevice = _load_sounddevice()

        try:
            stream = sounddevice.InputStream(
                device=self._device,
                samplerate=self._requested_rate,
                channels=self._requested_channels,
                dtype="int16",
            )
            stream.start()
        except Exception as exc:
            raise MicrophonePermissionError(
                tcc_message(
                    "Microphone",
                    self._device if self._device is not None else "default",
                    also="the device may be missing or held by another application",
                )
            ) from exc

        self._stream = stream
        # What the device actually gave, which is frequently not what was
        # asked for. Everything downstream is built from this.
        self._format = AudioFormat(
            sample_rate_hz=int(stream.samplerate),
            channels=int(stream.channels),
            encoding=PCM_S16LE,
        )

        if self._format.sample_rate_hz != self._requested_rate:
            log.info(
                "microphone opened at %d Hz, not the %d Hz requested; declaring the real rate",
                self._format.sample_rate_hz,
                self._requested_rate,
            )
        return self._format

    def read(self, seconds: float) -> AudioChunk:
        if self._stream is None or self._format is None:
            raise MicrophoneUnavailableError("read before open")

        frames = int(self._format.sample_rate_hz * seconds)
        data, overflowed = self._stream.read(frames)
        if overflowed:
            log.warning("microphone overflowed; some audio was dropped")

        return AudioChunk(pcm=data.tobytes(), format=self._format)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def _load_sounddevice():
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        # OSError too: sounddevice imports PortAudio at import time and
        # raises OSError when the library is missing, which is a missing
        # dependency wearing a different exception.
        raise MicrophoneDependencyError(
            "sounddevice is not installed, or PortAudio is missing. It is an optional "
            "dependency because only a machine with a microphone needs it: "
            "uv sync --extra laptop"
        ) from exc
    return sounddevice
