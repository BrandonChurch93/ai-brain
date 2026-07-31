# Runbook: the laptop body

The machine you are sitting at, presented to the brain as a body. Camera and
microphone today; the speaker follows in checklist step 4.3.

## Running it

Two processes. The brain first, then the body.

```
export BRAIN_AUTH_TOKEN=dev
uv sync --extra laptop          # OpenCV and sounddevice, needed only here
PYTHONPATH=src uv run laptop-body
```

`PYTHONPATH=src` is required and not optional. On macOS `uv` writes its
editable-install `.pth` file with the `UF_HIDDEN` flag, and CPython's `site`
module silently skips hidden `.pth` files, so `src/` never reaches
`sys.path`. Proper packaging retires both this and the schema path-walk
together, which is recorded against checklist step 7.3.

Body-side configuration, all `BRAIN_` prefixed:

| Variable | Meaning |
|---|---|
| `BRAIN_URL` | Where the brain is listening. Default `ws://127.0.0.1:8765`. |
| `BRAIN_AUTH_TOKEN` | Shared secret. The body presents it, the brain compares it. |
| `BRAIN_BODY_ID` | Defaults to `laptop-01`. |
| `BRAIN_LOG_LEVEL` | Defaults to `INFO`. |

## Camera and microphone permissions, once per program that asks

macOS gates both through TCC, and **the grant belongs to the program that
asks, not to this project**. Running the body from Terminal grants Terminal.
Running it from an IDE grants the IDE. They are tracked separately, and the
camera and microphone are separate grants again, so you may be asked twice.

The body opens both devices **at startup**, before it connects. That is
deliberate: any prompt appears while you are launching it, attached to an
action you just took, rather than in the middle of a mission when something
finally asks for a picture.

Two things worth knowing in advance:

- **The dialog belongs to the foreground app.** Started from a background
  process, a CI job, or an agent session, there may be nobody to see it. The
  device then fails to open, or waits on a prompt nobody answers.
- **A refusal looks like a missing device.** Neither OpenCV nor PortAudio can
  tell the two apart, so the error names the permission first, because that
  is the likeliest cause and the one with an action attached.

**A device that will not open is left out of the manifest.** The body still
starts, still connects, and simply does not declare that capability. This is
on purpose: a manifest naming a camera the body cannot use is a promise the
planner would build plans around. Check the log at startup, which says
plainly which capability was dropped and why.

To grant or check them:

**System Settings → Privacy & Security → Camera**, and again under
**Microphone**, then enable the program you run the body from.

To revoke and see the failure path for yourself:

```
tccutil reset Camera            # clears the grant for every app
tccutil reset Microphone
```

## Testing with real hardware

The suite never touches either device. Every test runs against stub sources:
a fixed JPEG and a generated tone. That is also what CI uses, since a runner
has no devices and nobody to answer a dialog.

The tests that use real hardware are skipped unless asked for:

```
BRAIN_CAMERA_TESTS=1 uv run pytest -m camera
```

Run them after granting the permissions. The first run may still show a
dialog if the terminal you are in has not asked before.

## What the body does

- Declares `sys`, plus `cam0` and `mic0` for whichever devices opened, and
  boots into `ok`. Neither a camera nor a microphone is actuation as SPEC
  section 7.1 defines it, and a hold protecting nothing would be cleared by
  rote at the start of every session.
- Declares `snapshot` alone. `start_stream` and `stop_stream` are in the
  camera class registry and are not implemented, and a manifest listing them
  would be a promise the body cannot keep.
- On `snapshot`: takes one picture, emits it as a `frame` event carrying the
  command's `trace_id`, and returns a result describing the shape. The image
  travels on the frame channel where perception belongs; repeating the base64
  in the result would double the cost of the largest payload v1 sends.
- The requested snapshot is **not** droppable. SPEC section 6.5 marks
  streamed frames droppable because another follows; a snapshot was asked
  for, and dropping it answers a question with silence.
- On `start_capture`: begins recording and emits an `audio_chunk` event every
  250ms until `stop_capture`. The result returns as soon as capture is
  running, not when it ends: a command whose result waited for the operator
  to let go of the button would outlive its own TTL every time.
- **Audio chunks are not droppable either.** A dropped chunk is a hole in a
  sentence, and the next one does not fill it.
- **Every chunk carries its own sample rate, channels and encoding**, not
  just the manifest. A recording is read back long after the manifest
  scrolled past, and PCM whose rate you have to go and look up is PCM you
  can get wrong. Step 6.1 hands this audio to Whisper, which cares.
- **Declared attributes are what the devices actually opened at.** Ask a
  camera for 1280x720 and it may give 640x480; ask a microphone for 16 kHz
  mono and it may give 48 kHz stereo. Both do so silently. The manifest
  reports what came back, so the log will not always match the defaults in
  the code, and that is correct.
- Honours E-stop. A camera cannot hurt anyone, but a body that ignored a
  global stop would report `ok` while everything else was stopped, which is
  worse than useless in a log.

## When something goes wrong

| What you see | What it usually is |
|---|---|
| `camera 0 would not start` | Permission not granted to the program you ran it from, or another app holds the camera. |
| `microphone default would not start` | Same, under Microphone. |
| `no camera capability` / `no microphone capability` at startup | The device did not open, so it was left out of the manifest. The rest of the line says why. |
| `opencv-python is not installed` | `uv sync --extra laptop`. |
| `sounddevice is not installed, or PortAudio is missing` | `uv sync --extra laptop`. |
| `ModuleNotFoundError: bodies` | `PYTHONPATH=src` was not set. See above. |
| Body connects, then `reject: auth_failed` | `BRAIN_AUTH_TOKEN` differs between the two processes. |
| Body connects, then nothing | The brain is up but not sending. Check its log; the body is waiting on its lease. |
