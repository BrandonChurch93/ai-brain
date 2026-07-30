"""Brain core: planning, perception, memory, safety, telemetry.

Owns everything that is not body-specific. Bodies attach through the
versioned adapter protocol in `wire`; nothing here knows what a body is
made of beyond its capability manifest (ADR-0001, ADR-0003).
"""
