# Body Adapter Protocol · v1 (SPEC)

- Status: Draft for V1 review
- Date: 2026-07-29
- Protocol version string: `2026-07-29`
- WebSocket subprotocol: `body-adapter-protocol.v1`
- Governing decisions: ADR-0002 (transport), ADR-0003 (capabilities), ADR-0004 (validator), ADR-0005 (logging), ADR-0006 (safety)
- Machine-readable source of truth: `protocol/schemas/protocol.schema.json`. Where prose and schema disagree, the schema wins and the prose is a bug.

## 1. Purpose and scope

This document defines how a body (any hardware or virtual device wrapped by an adapter) talks to the brain. It covers the transport, the session lifecycle, the message envelope, every message type, the capability model, and the safety semantics. It does not cover brain internals (planning, memory, reflex tiers), which are free to change without touching this contract.

## 2. Terminology

| Term | Meaning |
|---|---|
| Brain | The orchestration core. WebSocket server. |
| Body | One device presenting capabilities. One WebSocket connection per body. |
| Adapter | The driver process that wraps a device and speaks this protocol. One adapter may host multiple bodies by opening one connection each. |
| Capability | One declared function of a body (a camera, a speaker, a drive), typed by a capability class. |
| Session | One accepted connection, from `welcome` to socket close. |

## 3. Transport and framing

1. Transport is WebSocket (RFC 6455). Bodies connect to the brain; the brain never dials out. Brain URL is body-side config.
2. Clients MUST request the subprotocol `body-adapter-protocol.v1`. The subprotocol string names the compatibility family and changes only if a future, incompatible v2 family is ever created. Within the family, finer versioning happens in the handshake (section 5).
3. v1 uses JSON text frames only, UTF-8. Binary frames are reserved for a future CBOR data plane and MUST be ignored (not fatal) if received.
4. Deployment beyond localhost SHOULD use WSS. v1 development on one machine MAY use plain WS.

## 4. Envelope

Every message is a JSON object with this envelope. Payload shape depends on `type`.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `type` | string | yes | One of the 12 message types (section 6). |
| `id` | string | yes | Unique message id (ULID or UUIDv7 recommended). |
| `session` | string | after handshake | Session id issued in `welcome`. Absent only on `hello` and `reject`. |
| `seq` | integer | yes | Per-sender counter, starts at 1 per connection, increments by 1 per message sent. |
| `ts` | object | yes | `{ "mono_ns": <sender monotonic nanoseconds>, "utc": <ISO 8601> }`. This is t_captured. Receivers log their own t_received. |
| `trace_id` | string | no | Correlates everything belonging to one goal. |
| `span_id` | string | on commands | Correlates one command with its results. Required on `command` and `command_result`. |
| `payload` | object | yes | Type-specific content. |

Rules:

- Receivers MUST ignore unknown envelope or payload fields (forward compatibility).
- Senders MUST NOT repurpose an existing field name for a new meaning, ever. Retired names are documented as reserved in the schema and never reused.
- Ordering is guaranteed per sender per session by `seq`. Cross-device ordering uses brain-side t_received; clock skew of tens of milliseconds between devices is expected and MUST be tolerated.

## 5. Session lifecycle

```
body                                brain
  |------------- hello -------------->|   versions, auth, manifest
  |<------------ welcome -------------|   chosen version, session, heartbeat config
  |            (or reject)            |   then socket close
  |<===== steady state: heartbeat, event, command, command_result,
          list_capabilities, manifest, estop, estop_clear, error =====>|
```

Version negotiation (MCP-style):

1. `hello.protocol_versions` lists every version string the body supports, newest first. Version strings are dates (`YYYY-MM-DD`) and are bumped only by a backward-incompatible change.
2. If the brain supports any offered version it replies `welcome` with the single chosen version (the newest common one). Otherwise it replies `reject` with code `unsupported_version` and its supported list, then closes.
3. Effective capability is the intersection: both sides MUST behave per the chosen version and MUST NOT use features from newer versions.

Authentication: `hello.auth_token` is a shared secret from body-side config, compared constant-time by the brain. Failure produces `reject` with code `auth_failed`, then close. Localhost development MAY use a trivial token; anything beyond one machine gets a real secret and WSS.

Reconnection: any disconnect ends the session. The body reconnects with a fresh `hello` (new session, seq resets). Latched safety states (section 8) survive reconnection: a body in `safe_hold` or `estopped` reports that state in its first heartbeat and stays there until explicitly cleared.

## 6. Message types

Direction key: B→b is brain to body, b→B is body to brain.

| Type | Direction | Purpose |
|---|---|---|
| `hello` | b→B | Open a session: versions, auth, full manifest. |
| `welcome` | B→b | Accept: chosen version, session id, heartbeat config, recognized capabilities. |
| `reject` | B→b | Refuse: code, message, supported versions. Socket closes after. |
| `heartbeat` | both | Liveness plus current state, on the interval from `welcome`. |
| `event` | b→B | Perception, telemetry, state changes. |
| `command` | B→b | One action on one capability, with TTL. |
| `command_result` | b→B | Progress and terminal outcome for a command. |
| `list_capabilities` | B→b | Ask for a fresh manifest. |
| `manifest` | b→B | The manifest, in reply to `list_capabilities`. |
| `estop` | both | Emergency stop. Highest priority, bypasses queues. |
| `estop_clear` | B→b | Explicit release of a latched stop. |
| `error` | both | Protocol-level problem, references the offending message id. |

### 6.1 `hello` payload

```json
{
  "protocol_versions": ["2026-07-29"],
  "auth_token": "<secret>",
  "manifest": { ...see section 7... }
}
```

### 6.2 `welcome` payload

```json
{
  "protocol_version": "2026-07-29",
  "session": "sess_01J1...",
  "server": { "name": "brain", "version": "0.1.0" },
  "utc_now": "2026-07-29T18:00:00.000Z",
  "heartbeat": { "interval_ms": 1000, "lease_ms": 3000 },
  "capability_ack": { "recognized": ["sys", "cam0"], "unrecognized": [] }
}
```

`utc_now` lets a clockless microcontroller seed a wall-clock estimate. `unrecognized` capabilities remain reachable only through `do_command`.

### 6.3 `reject` payload

```json
{ "code": "unsupported_version", "message": "...", "supported": ["2026-07-29"] }
```

Codes: `unsupported_version`, `auth_failed`, `malformed_hello`.

### 6.4 `heartbeat` payload

Body to brain: `{ "state": "ok" | "safe_hold" | "estopped", "uptime_ms": 123456 }`
Brain to body: `{ "state": "active" | "degraded" }`

Both sides send on `interval_ms`. Missing heartbeats for `lease_ms` is a lease miss (section 8).

### 6.5 `event` payload

```json
{
  "capability": "cam0",
  "event": "frame",
  "data": { "format": "jpeg", "b64": "...", "width": 1280, "height": 720 },
  "droppable": true
}
```

`event` names come from the capability class registry (section 7.2). `droppable: true` marks data the sender MAY skip under backpressure (frames), as opposed to data that MUST arrive (state changes). Media travels base64-inside-JSON in v1, at modest rates by design; sustained high-rate streaming is the reserved CBOR/binary seam, not a v1 feature.

Reserved event on every body: `event: "state"` from capability `sys`, emitted on every body-state transition with `data: { "state": ..., "cause": ... }`.

### 6.6 `command` payload

```json
{
  "capability": "spk0",
  "action": "say",
  "params": { "text": "Hello Brandon." },
  "ttl_ms": 5000,
  "expects_result": true
}
```

Rules:

- `ttl_ms` is REQUIRED on every command. A command not begun within its TTL (measured by the body from its own receipt time) MUST NOT execute and MUST produce a terminal `expired` result. This is the lifespan rule that makes a stalled network inherently safe.
- Envelope `span_id` is REQUIRED. Bodies MUST deduplicate by `span_id` within a session: a retransmitted command executes at most once.
- The brain sends commands only after its validator has passed them (ADR-0004); the body still enforces its own checks and MAY reject.

### 6.7 `command_result` payload

```json
{ "status": "succeeded", "progress": 1.0, "data": {}, "error": null }
```

Statuses: non-terminal `accepted`, `running` (repeatable, with `progress` 0..1); terminal `succeeded`, `failed`, `expired`, `rejected` (exactly one terminal result per span, echoing the command's `span_id` and `trace_id`). `error` carries `{ "code", "message" }` on `failed` and `rejected`.

Standard result error code: **`interrupted`**, on a `failed` result, for a long-running action stopped or displaced before completion (a spoken sentence cut off, an action superseded by a later one). The terminal set has no cancellation status by design, so this is how a body says "not a fault, but it did not finish". Bodies use this code rather than inventing synonyms.

### 6.8 `list_capabilities` payload

`{}`. Body replies with a `manifest` message.

### 6.9 `estop` payload

`{ "reason": "operator voice command" }`

Semantics in section 8. Bodies may also send `estop` to report a locally triggered stop.

### 6.10 `estop_clear` payload

`{ "reason": "obstacle removed", "operator": "brandon" }`

### 6.11 `error` payload

`{ "code": "unknown_action", "message": "...", "ref_id": "<offending message id>" }`

Codes: `malformed`, `unknown_type`, `unknown_capability`, `unknown_action`, `invalid_params`, `busy`, `latched_safe_state`, `internal`.

## 7. Capability model

### 7.1 Manifest

```json
{
  "body_id": "laptop-01",
  "display_name": "MacBook body",
  "hardware_class": "workstation",
  "boot_state": "ok",
  "adapter": { "name": "laptop-adapter", "version": "0.1.0" },
  "capabilities": [
    {
      "id": "cam0",
      "class": "camera",
      "attributes": { "formats": ["jpeg"], "max_fps": 5, "resolutions": ["1280x720"] },
      "actions": ["snapshot", "start_stream", "stop_stream"],
      "events": ["frame"],
      "do_command": true
    }
  ]
}
```

- `hardware_class`: `workstation`, `sbc`, `microcontroller`, `mobile_base`, `virtual`.
- `boot_state`: the state a freshly started body process enters. Bodies with actuation MUST boot into `safe_hold` (motion requires an explicit clear); sensor-only bodies MAY boot into `ok`.
  - For `boot_state`, **actuation** means a capability class that produces physical motion or mechanical effect. In v1 that is exactly `differential_drive`; a future motion class joins this list explicitly when it is added. Output classes such as `speaker` and `display` are not actuation for this purpose, so a body whose capabilities are only sensing or output MAY boot `ok`.
- `capabilities[].actions` and `.events` MUST be subsets of the class registry entries for that class. Extra, nonstandard functionality is reached through `do_command` (action name `do_command`, free-form `params`), which every capability supports implicitly. That is the escape hatch: nonstandard features never fork the protocol.
- The manifest is the planner's affordance set. The brain's validator refuses any command whose capability id, action, or param bounds are not covered by the current manifest (ADR-0003, ADR-0004).

### 7.2 Capability class registry, v1

The registry grows additively; a new class or a new optional action is not a version bump.

| Class | Standard actions | Standard events | Core attributes |
|---|---|---|---|
| `system` (required on every body, id `sys`) | `ping`, `clear_safe_hold` | `state`, `log` | none |
| `camera` | `snapshot`, `start_stream`, `stop_stream` | `frame` | `formats`, `resolutions`, `max_fps` |
| `microphone` | `start_capture`, `stop_capture` | `audio_chunk` | `sample_rate_hz`, `channels`, `encoding` |
| `speaker` | `say` (text, synthesized body-side or brain-side per attributes), `play` (audio payload), `stop` | `playback_state` | `tts`: `"local"` or `"none"` |
| `display` | `show_text`, `clear` | none | `columns`, `rows` |
| `differential_drive` | `set_velocity` (linear m/s, angular rad/s), `stop` | `odometry` | `max_linear_mps`, `max_angular_rps` |
| `range_sensor` | `read` | `range` | `min_m`, `max_m`, `fov_deg` |
| `virtual` | none standard | none standard | free-form; interact via `do_command` |

`differential_drive` and `range_sensor` exist in v1 solely so the mock body can exercise actuation semantics (TTL, safe_hold, E-stop) with zero hardware.

## 8. Safety semantics

These implement ADR-0006 on the wire.

1. **Heartbeat lease.** If a body sees no brain heartbeat for `lease_ms`, it MUST enter `safe_hold`: all actuation stops, in-flight actuation spans end with terminal `failed` (`code: "latched_safe_state"`), and the state latches. If the brain sees no body heartbeat for `lease_ms`, it marks the body LOST, fails outstanding spans brain-side, and replans.
2. **Latching.** `safe_hold` and `estopped` never clear themselves, not on reconnect, not on timeout. `safe_hold` clears only via a `clear_safe_hold` command to `sys`. `estopped` clears only via `estop_clear`. Silent resume is prohibited.
   - `estop_clear` transitions a body to `safe_hold`, never directly to `ok`: restoring motion takes two explicit authorizations, so no single message both releases an emergency stop and re-enables movement.
   - `estopped` outranks `safe_hold`. A lease miss while estopped MUST NOT downgrade the state, and `clear_safe_hold` alone MUST NOT clear it; otherwise clearing the milder latch would release a stop nobody cleared.
3. **E-stop priority.** `estop` bypasses normal processing: receivers MUST act on it immediately upon parse, ahead of any queued work, and senders MUST give it a send path that cannot sit behind backpressured writes. On receipt: cease all actuation, enter `estopped`, emit a `sys` state event, fail all in-flight actuation spans.
4. **Motion is permissioned.** Stopped is the default. Actuation requires all of: a non-latched state, a validator-passed command, and an unexpired TTL. Absence of any one means stillness.
5. **Degraded brain.** The brain's DEGRADED mode (ADR-0004) is visible to bodies only as `heartbeat.state: "degraded"`; bodies MAY treat it as advisory. Brain internals decide what degraded means; the wire contract does not change.

## 9. Versioning and evolution

1. Payload schemas are additive-only. New fields ship with safe defaults; receivers already ignore what they do not know.
2. A field, once shipped, never changes meaning or type. Removal means marking it reserved in the schema, forever.
3. The date version bumps only on a genuinely breaking change, after exhausting additive options. The brain (as the long-lived server) upgrades first and MUST keep accepting every version inside its supported window, published in `reject.supported`.
4. If a breaking change is truly unavoidable, both versions run in parallel through negotiation until every body migrates. Logged sessions replay under the version they were recorded with (ADR-0005).

## 10. Adapter conformance checklist

An adapter is conformant when it: ignores unknown fields everywhere; sends `hello` with a truthful manifest including `sys`; heartbeats on the configured interval and reacts to lease misses by latching `safe_hold`; enforces command TTL locally; deduplicates by `span_id`; emits exactly one terminal result per span; processes `estop` ahead of all queued work; latches and never self-clears; and emits a `sys` state event on every state transition. The mock body is the reference implementation of this checklist, and the test suite asserts every line of it.

## 11. Example session (abridged)

```json
→ {"type":"hello","id":"01A","seq":1,"ts":{"mono_ns":1,"utc":"2026-07-29T18:00:00Z"},
   "payload":{"protocol_versions":["2026-07-29"],"auth_token":"dev",
              "manifest":{"body_id":"laptop-01","hardware_class":"workstation","boot_state":"ok",
                          "adapter":{"name":"laptop-adapter","version":"0.1.0"},
                          "capabilities":[
                            {"id":"sys","class":"system","actions":["ping","clear_safe_hold"],"events":["state","log"]},
                            {"id":"spk0","class":"speaker","attributes":{"tts":"local"},"actions":["say","stop"],"events":["playback_state"]}]}}}

← {"type":"welcome","id":"01B","session":"sess_1","seq":1,"ts":{"mono_ns":9,"utc":"2026-07-29T18:00:00Z"},
   "payload":{"protocol_version":"2026-07-29","session":"sess_1",
              "server":{"name":"brain","version":"0.1.0"},"utc_now":"2026-07-29T18:00:00.050Z",
              "heartbeat":{"interval_ms":1000,"lease_ms":3000},
              "capability_ack":{"recognized":["sys","spk0"],"unrecognized":[]}}}

← {"type":"command","id":"01C","session":"sess_1","seq":2,"ts":{"mono_ns":10,"utc":"..."},
   "trace_id":"trc_greet","span_id":"spn_1",
   "payload":{"capability":"spk0","action":"say","params":{"text":"Hello."},"ttl_ms":5000,"expects_result":true}}

→ {"type":"command_result","id":"01D","session":"sess_1","seq":2,"ts":{"mono_ns":22,"utc":"..."},
   "trace_id":"trc_greet","span_id":"spn_1",
   "payload":{"status":"succeeded","progress":1.0,"data":{},"error":null}}
```

The V1 acceptance scenario in STATE.md ("kill the socket mid-mission") exercises sections 6.6, 8.1, and 8.2 end to end.
