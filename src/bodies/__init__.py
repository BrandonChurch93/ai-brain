"""Body adapters: the drivers.

Each body is a separate process that connects to the brain as a protocol
client and declares what it can do via a capability manifest. Adding a body
must never require a change inside `brain` (ADR-0001).
"""
