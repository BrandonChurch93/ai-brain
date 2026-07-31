"""macOS permission failures, explained rather than hung on.

TCC gates the camera and the microphone, and the grant belongs to the
process that asks: Terminal, or an IDE, not this package. The first access
from a program that has never asked raises a dialog. Nothing here can grant
it, and in a background process or an agent session there may be nobody to
see it.

Two facts shape everything in this module. An unanswered prompt is
indistinguishable from a hang, so capture must give up quickly. And the
capture libraries cannot tell a refusal from a missing device, so the
message says both rather than claiming to know which.
"""

from __future__ import annotations

SETTINGS_PATH = "System Settings > Privacy & Security"
RUNBOOK = "docs/runbook-laptop-body.md"


def tcc_message(kind: str, device: object, *, also: str) -> str:
    """Explain a device that would not start, likeliest cause first.

    `kind` is the TCC pane name, capitalised as macOS spells it, so the text
    can be followed literally rather than translated.
    """
    return (
        f"{kind.lower()} {device} would not start. On macOS the usual cause is the "
        f"{kind.lower()} permission: the grant belongs to the process that asks, so "
        f"Terminal or your IDE needs it under {SETTINGS_PATH} > {kind}. The first "
        f"access from a new process shows a prompt, and an unanswered prompt looks "
        f"exactly like this. Otherwise {also}. See {RUNBOOK}"
    )
