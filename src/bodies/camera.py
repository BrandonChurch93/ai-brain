"""Camera capture, behind an interface the tests can replace.

The real source opens a webcam. The stub returns a fixed JPEG. Everything
above this file is identical either way, which is what lets the adapter
conformance suite run a laptop body in CI on a machine with no camera and no
one to click an permission dialog.

macOS gates camera access through TCC, and the grant belongs to the process
that asks: Terminal, or the IDE, not this package. The first real capture
from a new process pops a dialog. Answer it and it is remembered; ignore it
and the capture hangs. Nothing here can grant it, so the job here is to fail
quickly and say exactly what is wrong rather than block forever on a prompt
nobody can see. See `docs/runbook-laptop-body.md`.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("bodies.camera")

#: Modest by design (SPEC section 6.5): v1 carries media as base64 inside
#: JSON, and sustained high-rate streaming is the reserved binary seam.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_JPEG_QUALITY = 70

#: How long to wait on a device before giving up. Short on purpose: the
#: usual reason a webcam does not open on macOS is an unanswered permission
#: dialog, and waiting longer only makes that harder to recognise.
DEFAULT_OPEN_TIMEOUT_S = 5.0


class CameraError(RuntimeError):
    """The camera could not be used. The message says why."""


class CameraPermissionError(CameraError):
    """Access was refused, or a permission prompt was never answered."""


class CameraUnavailableError(CameraError):
    """No usable device, or the driver would not produce a frame."""


class CameraDependencyError(CameraError):
    """The optional capture dependency is not installed."""


@dataclass(frozen=True, slots=True)
class Frame:
    """One captured image, already JPEG-encoded."""

    jpeg: bytes
    width: int
    height: int

    @property
    def b64(self) -> str:
        return base64.b64encode(self.jpeg).decode("ascii")

    def as_event_data(self) -> dict[str, object]:
        """The `frame` event payload shape from SPEC section 6.5."""
        return {
            "format": "jpeg",
            "b64": self.b64,
            "width": self.width,
            "height": self.height,
        }


class CaptureSource(Protocol):
    """Where frames come from."""

    def open(self) -> None: ...

    def capture(self) -> Frame: ...

    def close(self) -> None: ...


#: A real 16x12 JPEG. Embedded rather than generated so the stub needs no
#: image library, and so every run produces identical bytes: a replayed
#: session must reproduce its decisions, and a camera that invented new
#: pixels each time would make that untestable (ADR-0005).
_STUB_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcv"
    "Jik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7"
    "Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAAMABADASIAAhEBAxEB/8QAHwAAAQUBAQEB"
    "AQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKB"
    "kaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1"
    "dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl"
    "5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcF"
    "BAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0"
    "tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDmaKKK9o8g"
    "/9k="
)


class StubCamera:
    """A camera that is always there and always returns the same picture.

    Not a mock in the test-double sense: it satisfies the same interface and
    is the source the conformance suite and CI use, so the laptop body is
    exercised end to end without hardware.
    """

    __slots__ = ("_frame", "_opened", "captures")

    def __init__(self, frame: Frame | None = None) -> None:
        self._frame = frame if frame is not None else stub_frame()
        self._opened = False
        self.captures = 0

    def open(self) -> None:
        self._opened = True

    def capture(self) -> Frame:
        if not self._opened:
            raise CameraUnavailableError("capture before open")
        self.captures += 1
        return self._frame

    def close(self) -> None:
        self._opened = False


def stub_frame() -> Frame:
    return Frame(jpeg=base64.b64decode(_STUB_JPEG_B64), width=16, height=12)


class OpenCVCamera:
    """A real webcam, via OpenCV.

    Imported lazily. OpenCV is a large dependency that only a machine with a
    camera needs, and CI has neither.
    """

    __slots__ = ("_capture", "_device", "_height", "_quality", "_timeout_s", "_width")

    def __init__(
        self,
        device: int = 0,
        *,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        quality: int = DEFAULT_JPEG_QUALITY,
        timeout_s: float = DEFAULT_OPEN_TIMEOUT_S,
    ) -> None:
        self._device = device
        self._width = width
        self._height = height
        self._quality = quality
        self._timeout_s = timeout_s
        self._capture = None

    def open(self) -> None:
        cv2 = _load_cv2()

        capture = cv2.VideoCapture(self._device)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)

        if not capture.isOpened():
            capture.release()
            raise CameraPermissionError(_denied_message(self._device))

        self._capture = capture
        log.info("camera %s open", self._device)

    def capture(self) -> Frame:
        if self._capture is None:
            raise CameraUnavailableError("capture before open")

        cv2 = _load_cv2()

        ok, frame = self._capture.read()
        if not ok or frame is None:
            raise CameraPermissionError(_denied_message(self._device))

        encoded, buffer = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self._quality]
        )
        if not encoded:
            raise CameraUnavailableError("the driver returned a frame that would not encode")

        height, width = frame.shape[:2]
        return Frame(jpeg=buffer.tobytes(), width=int(width), height=int(height))

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None


def _load_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise CameraDependencyError(
            "opencv-python is not installed. It is an optional dependency because "
            "only a machine with a camera needs it: uv sync --extra laptop"
        ) from exc
    return cv2


def _denied_message(device: int) -> str:
    """Name the permission, because the alternative is a silent hang.

    OpenCV cannot distinguish "refused by TCC" from "no such device": both
    surface as a capture that will not open or will not read. Rather than
    guess, this says what is true and what to check, most likely cause first.
    """
    return (
        f"camera {device} would not produce a frame. On macOS the usual cause is the "
        f"camera permission: the grant belongs to the process that asks, so Terminal "
        f"or your IDE needs it under System Settings > Privacy & Security > Camera. "
        f"The first capture from a new process shows a prompt, and an unanswered "
        f"prompt looks exactly like this. Otherwise the device may be missing or in "
        f"use by another application. See docs/runbook-laptop-body.md"
    )
