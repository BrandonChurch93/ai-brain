"""The protocol fixture suite.

Twenty-seven wire messages on disk: eighteen the schema must accept, nine it
must reject. This suite is permanent. Run it after any change near the wire
format (CLAUDE.md, "How we work").

Fixtures carry `_comment` / `_violates` keys explaining themselves. Those are
unknown fields, so the schema accepts them, which is itself the forward
compatibility rule working (SPEC section 4).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wire import ProtocolValidationError, is_valid, iter_errors, validate_message

FIXTURES = Path(__file__).parent / "fixtures" / "protocol"
VALID_DIR = FIXTURES / "valid"
INVALID_DIR = FIXTURES / "invalid"

EXPECTED_VALID = 18
EXPECTED_INVALID = 9

#: The schema keyword each invalid fixture must trip. Counting failures is not
#: enough: a fixture with a typo would fail for the wrong reason and still pass
#: a bare count, quietly retiring the rule it was written to defend.
EXPECTED_VIOLATION = {
    "01-command-missing-ttl": "required",
    "02-command-missing-span-id": "required",
    "03-hello-bad-version-format": "pattern",
    "04-heartbeat-missing-session": "required",
    "05-command-result-bad-status": "enum",
    "06-estop-empty-reason": "minLength",
    "07-manifest-malformed-body-id": "pattern",
    "08-unknown-message-type": "enum",
    "09-event-missing-ts": "required",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixtures(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


VALID_FIXTURES = _fixtures(VALID_DIR)
INVALID_FIXTURES = _fixtures(INVALID_DIR)


def test_fixture_counts() -> None:
    """The suite is 18 and 9. A dropped fixture is a silently weakened suite."""
    assert len(VALID_FIXTURES) == EXPECTED_VALID
    assert len(INVALID_FIXTURES) == EXPECTED_INVALID


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda path: path.stem)
def test_valid_fixture_passes(path: Path) -> None:
    message = _load(path)
    errors = iter_errors(message)
    assert not errors, "\n".join(
        f"{list(error.absolute_path)}: {error.message}" for error in errors
    )
    validate_message(message)
    assert is_valid(message)


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda path: path.stem)
def test_invalid_fixture_fails(path: Path) -> None:
    message = _load(path)
    assert not is_valid(message), f"{path.name} was accepted but must be rejected"

    with pytest.raises(ProtocolValidationError):
        validate_message(message)

    tripped = {error.validator for error in iter_errors(message)}
    assert EXPECTED_VIOLATION[path.stem] in tripped, (
        f"{path.name} failed, but on {sorted(tripped)} rather than "
        f"the {EXPECTED_VIOLATION[path.stem]} rule it exists to defend"
    )


def test_every_message_type_is_covered() -> None:
    """The valid fixtures exercise all twelve types, not just the easy ones."""
    from wire import message_types

    covered = {_load(path)["type"] for path in VALID_FIXTURES}
    assert covered == message_types()
