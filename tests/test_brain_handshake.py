"""Handshake rules, without sockets (SPEC section 5)."""

from __future__ import annotations

import pytest

from brain.config import ConfigError, ServerConfig
from brain.handshake import Accepted, Refused, authenticate, choose_version, open_session
from wire import HelloEnvelope, decode_object, to_object
from wire.schema import protocol_version, supported_versions
from wire.stamp import SeqCounter


def hello_message(
    *,
    token: str = "dev",
    versions: list[str] | None = None,
    body_id: str = "mock-01",
) -> HelloEnvelope:
    message = decode_object(
        {
            "type": "hello",
            "id": "01JZQK8N4T00000000000000A1",
            "seq": 1,
            "ts": {"mono_ns": 1, "utc": "2026-07-29T18:00:00.000Z"},
            "payload": {
                "protocol_versions": versions if versions is not None else [protocol_version()],
                "auth_token": token,
                "manifest": {
                    "body_id": body_id,
                    "hardware_class": "virtual",
                    "boot_state": "safe_hold",
                    "adapter": {"name": "mock-adapter", "version": "0.1.0"},
                    "capabilities": [
                        {"id": "sys", "class": "system", "actions": ["ping"]},
                        {"id": "drive0", "class": "differential_drive", "actions": ["stop"]},
                    ],
                },
            },
        }
    )
    assert isinstance(message, HelloEnvelope)
    return message


def run(hello: HelloEnvelope, *, token: str = "dev", supported: tuple[str, ...] | None = None):
    return open_session(
        hello,
        auth_token=token,
        heartbeat_interval_ms=1000,
        heartbeat_lease_ms=3000,
        server_version="0.1.0",
        seq=SeqCounter(),
        supported=supported,
    )


def test_good_hello_is_accepted() -> None:
    outcome = run(hello_message())
    assert isinstance(outcome, Accepted)
    assert outcome.protocol_version == protocol_version()
    assert outcome.body_id == "mock-01"
    assert outcome.welcome.payload.session == outcome.session
    assert outcome.welcome.session == outcome.session


def test_welcome_is_a_legal_message() -> None:
    """The boundary validates on the way out, so this also proves the brain
    cannot emit a welcome it would itself refuse."""
    outcome = run(hello_message())
    assert isinstance(outcome, Accepted)
    assert to_object(outcome.welcome)["type"] == "welcome"


def test_welcome_acknowledges_every_declared_capability() -> None:
    outcome = run(hello_message())
    assert isinstance(outcome, Accepted)
    ack = outcome.welcome.payload.capability_ack
    assert ack is not None
    assert ack.recognized == ["sys", "drive0"]


def test_wrong_token_is_refused_as_auth_failed() -> None:
    outcome = run(hello_message(token="wrong"))
    assert isinstance(outcome, Refused)
    assert outcome.reject.payload.code == "auth_failed"


def test_auth_failure_does_not_disclose_supported_versions() -> None:
    """An unauthorised caller learns nothing about this brain."""
    outcome = run(hello_message(token="wrong"))
    assert isinstance(outcome, Refused)
    assert "supported" not in to_object(outcome.reject)["payload"]


def test_unknown_version_is_refused_with_the_supported_list() -> None:
    outcome = run(hello_message(versions=["2099-01-01"]))
    assert isinstance(outcome, Refused)
    assert outcome.reject.payload.code == "unsupported_version"
    assert outcome.reject.payload.supported == list(supported_versions())


def test_reject_carries_no_session() -> None:
    """SPEC section 4: the handshake failed, so there is no session to name."""
    outcome = run(hello_message(token="wrong"))
    assert isinstance(outcome, Refused)
    assert "session" not in to_object(outcome.reject)


def test_an_unauthenticated_and_incompatible_body_hears_about_auth_first() -> None:
    outcome = run(hello_message(token="wrong", versions=["2099-01-01"]))
    assert isinstance(outcome, Refused)
    assert outcome.reject.payload.code == "auth_failed"


BOTH = ("2026-01-01", "2026-07-29")


def test_newest_common_version_wins() -> None:
    assert choose_version(list(BOTH), BOTH) == "2026-07-29"


def test_version_choice_does_not_trust_the_body_ordering() -> None:
    """The body claims newest-first; the brain sorts rather than believing it."""
    assert choose_version(list(BOTH), tuple(reversed(BOTH))) == "2026-07-29"
    assert choose_version(["2026-07-29", "2026-01-01"], ("2026-01-01",)) == "2026-01-01"


def test_no_common_version_is_none() -> None:
    assert choose_version(["2099-01-01"], ("2026-07-29",)) is None
    assert choose_version([], ("2026-07-29",)) is None


def test_authenticate_is_exact() -> None:
    assert authenticate("dev", "dev")
    assert not authenticate("dev", "devx")
    assert not authenticate("", "dev")
    assert authenticate("", "")


def test_supported_versions_come_from_the_schema() -> None:
    """Not written down twice: the version is read off the schema `$id`."""
    assert supported_versions() == (protocol_version(),)


def test_lease_shorter_than_interval_is_refused() -> None:
    """A lease at or under the interval latches every body immediately."""
    with pytest.raises(ConfigError):
        ServerConfig(heartbeat_interval_ms=1000, heartbeat_lease_ms=1000)


def test_config_reads_brain_prefixed_variables() -> None:
    config = ServerConfig.from_env(
        {"BRAIN_HOST": "0.0.0.0", "BRAIN_PORT": "9000", "BRAIN_AUTH_TOKEN": "secret"}
    )
    assert (config.host, config.port, config.auth_token) == ("0.0.0.0", 9000, "secret")


def test_config_rejects_a_non_numeric_port() -> None:
    with pytest.raises(ConfigError):
        ServerConfig.from_env({"BRAIN_PORT": "eight thousand"})
