"""Our MCAP files, judged by a tool that shares no code with the writer.

The `log_time` bug in step 2.2 passed every test in this suite and was found
by `mcap doctor`. The lesson generalises: where an independent verifier
exists for something we produce, it belongs in the suite, not just in
somebody's shell history.

Two traps this test is shaped to avoid.

`mcap doctor` exits 0 on a file whose log_time runs backwards. Asserting the
exit code alone would be worthless for exactly the bug that motivated this,
so the warning output is what gets asserted.

A test that skips when the CLI is missing is a test that quietly stops
running the day CI changes. `BRAIN_REQUIRE_MCAP_CLI=1`, which CI sets, turns
the skip into a failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from brain.recorder import FlightRecorder
from wire import Manifest, decode_object, message_types

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "protocol" / "valid").glob("*.json"))

REQUIRE_CLI_ENV = "BRAIN_REQUIRE_MCAP_CLI"

#: `doctor` warns that our profile is not one of the well-known ROS ones.
#: That is true and intended: this is a custom domain profile (ADR-0000).
EXPECTED_WARNINGS = ("is not a well-known profile",)


def mcap_cli() -> str:
    found = shutil.which("mcap")
    if found is not None:
        return found

    if os.environ.get(REQUIRE_CLI_ENV) == "1":
        pytest.fail(
            f"{REQUIRE_CLI_ENV}=1 but the mcap CLI is not on PATH. "
            f"This check must not silently skip where it is required."
        )
    pytest.skip("mcap CLI not installed; install it to run conformance checks")


@pytest.fixture(scope="module")
def session_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A session covering every message type, written the normal way."""
    path = tmp_path_factory.mktemp("mcap-cli") / "conformance.mcap"

    manifest = Manifest.model_validate(
        {
            "body_id": "mock-01",
            "hardware_class": "virtual",
            "boot_state": "safe_hold",
            "adapter": {"name": "mock-adapter", "version": "0.1.0"},
            "capabilities": [{"id": "sys", "class": "system", "actions": ["ping"]}],
        }
    )

    with FlightRecorder(path) as recorder:
        recorder.record_session(
            session="sess_conformance",
            body_id="mock-01",
            protocol_version="2026-07-29",
            manifest=manifest,
        )
        for position, fixture in enumerate(FIXTURES):
            recorder.record(
                "rx" if position % 2 == 0 else "tx",  # type: ignore[arg-type]
                decode_object(json.loads(fixture.read_text(encoding="utf-8"))),
                session="sess_conformance",
                body_id="mock-01",
            )

    return path


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [mcap_cli(), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_doctor_finds_nothing_wrong(session_file: Path) -> None:
    """The check that would have caught the step 2.2 log_time bug.

    Asserted on the warnings, not the exit code: doctor exits 0 on a file
    whose log_time runs backwards.
    """
    result = run("doctor", str(session_file))

    assert result.returncode == 0, result.stderr

    unexpected = [
        line
        for line in result.stderr.splitlines()
        if "Warning" in line and not any(known in line for known in EXPECTED_WARNINGS)
    ]
    assert not unexpected, "mcap doctor found problems:\n" + "\n".join(unexpected)


def test_doctor_would_actually_fail_on_a_broken_file(tmp_path: Path) -> None:
    """Proves the check above can fail. A conformance test that cannot
    distinguish a good file from a bad one is decoration."""
    broken = tmp_path / "broken.mcap"
    broken.write_bytes(b"this is not an MCAP file")

    assert run("doctor", str(broken)).returncode != 0


def test_info_reports_every_channel(session_file: Path) -> None:
    result = run("info", str(session_file))

    assert result.returncode == 0, result.stderr
    for message_type in message_types():
        assert f"/{message_type}" in result.stdout
    assert "/session_meta" in result.stdout
    assert "channels:    13" in result.stdout


def test_the_cli_can_read_every_message_back(session_file: Path) -> None:
    result = run("cat", str(session_file))

    assert result.returncode == 0, result.stderr
    # One session_meta record plus one per fixture.
    assert len(result.stdout.strip().splitlines()) == len(FIXTURES) + 1
