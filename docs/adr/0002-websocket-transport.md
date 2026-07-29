# ADR-0002: WebSocket control plane, not ROS 2/DDS

- Status: Accepted
- Date: 2026-07-29

## Context

Bodies will be heterogeneous (laptop, ESP32-class microcontrollers, Raspberry Pi, Jetson) on home Wi-Fi, built by a solo developer whose strengths are TypeScript and Python. Research on prior art found: ROS 2's DDS transport suffers discovery flooding and documented reliability failures over Wi-Fi even at small fleet sizes, plus heavy toolchain complexity and weak macOS support; MQTT requires a broker and makes request/reply awkward; Zenoh performs best on wireless but is a younger ecosystem; gRPC is heavier on microcontrollers. Full details and sources in the referenced survey.

## Decision

The brain-to-body control plane is WebSocket, carrying a small versioned message envelope (JSON now, CBOR option later for microcontrollers).

1. DDS's QoS vocabulary is stolen as envelope fields, not as a transport: per-stream reliability class, deadline, and lifespan/TTL.
2. Clean seams are reserved for MQTT (fleet fan-out) and Zenoh or gRPC (high-rate data plane) so a later addition touches the transport layer only.
3. ROS 2 is never the core. If its ecosystem (Nav2, SLAM, MoveIt) is ever needed, it joins as one bridge body speaking the adapter protocol.

## Consequences

We accept building our own pub/sub semantics and QoS handling in exchange for universal client support, trivial logging and inspection, firewall friendliness, and a perfect fit to existing skills. Revisit triggers: control-plane latency over Wi-Fi becomes a measured bottleneck, or the fleet exceeds roughly ten bodies or spans multiple networks.

## References

- docs/research/middleware-survey.md, sections 1, 2, and 7
