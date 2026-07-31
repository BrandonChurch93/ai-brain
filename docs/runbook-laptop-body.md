# Runbook: the laptop body

The machine you are sitting at, presented to the brain as a body. Camera
today; microphone and speaker follow in checklist steps 4.2 and 4.3.

## Running it

Two processes. The brain first, then the body.

```
export BRAIN_AUTH_TOKEN=dev
uv sync --extra laptop          # installs OpenCV, needed only here
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

## Camera permission, once per process that asks

macOS gates the camera through TCC, and **the grant belongs to the program
that asks, not to this project**. Running the body from Terminal grants
Terminal. Running it from an IDE grants the IDE. They are tracked separately,
so doing it in one place does not do it in the other.

The first capture from a program that has never asked raises a permission
dialog. Answer it and macOS remembers. Two things make this worth knowing in
advance:

- **The dialog belongs to the foreground app.** Started from a background
  process, a CI job, or an agent session, there may be nobody to see it. The
  capture then fails or waits on a prompt that is never answered.
- **A refusal looks like a missing camera.** OpenCV reports both the same
  way: a device that will not open, or a read that returns nothing.

So the camera capability does not wait. It gives up after a few seconds and
returns a `failed` result whose message names the permission first, because
that is the likeliest cause and the one with an action attached.

To grant or check it:

**System Settings → Privacy & Security → Camera**, then enable the program
you run the body from.

To revoke it and see the failure path for yourself:

```
tccutil reset Camera            # clears the grant for every app
```

## Testing with a real camera

The suite never touches the camera. Every test runs against a stub capture
source that returns a fixed JPEG, which is also what CI uses: there is no
camera on a runner and nobody to answer a dialog.

One test does use real hardware, and it is skipped unless asked:

```
BRAIN_CAMERA_TESTS=1 uv run pytest -m camera
```

Run it after granting the permission. The first run may still show the
dialog if the terminal you are in has not asked before.

## What the body does

- Declares `sys` and `cam0`, and boots into `ok`. It has no actuation, and
  SPEC section 7.1 requires `safe_hold` only of a body that can move. A hold
  protecting nothing would be cleared by rote at the start of every session.
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
- Honours E-stop. A camera cannot hurt anyone, but a body that ignored a
  global stop would report `ok` while everything else was stopped, which is
  worse than useless in a log.

## When something goes wrong

| What you see | What it usually is |
|---|---|
| `camera 0 would not produce a frame` | Permission not granted to the program you ran it from, or another app holds the camera. |
| `opencv-python is not installed` | `uv sync --extra laptop`. |
| `ModuleNotFoundError: bodies` | `PYTHONPATH=src` was not set. See above. |
| Body connects, then `reject: auth_failed` | `BRAIN_AUTH_TOKEN` differs between the two processes. |
| Body connects, then nothing | The brain is up but not sending. Check its log; the body is waiting on its lease. |
