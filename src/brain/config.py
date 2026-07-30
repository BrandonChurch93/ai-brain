"""Server configuration, from the environment.

Every variable is BRAIN_-prefixed (ADR-0000 rule 3). The port is config, not
protocol: SPEC leaves the brain URL to body-side config and names no port.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Heartbeat defaults the brain hands each body in `welcome` (SPEC section 6.2).
# The lease is three intervals: one missed heartbeat is a hiccup, three is a
# body that has stopped talking (ADR-0006).
DEFAULT_HEARTBEAT_INTERVAL_MS = 1000
DEFAULT_HEARTBEAT_LEASE_MS = 3000

# How long a body has to send `hello` after the socket opens.
DEFAULT_HANDSHAKE_TIMEOUT_S = 10.0


class ConfigError(RuntimeError):
    """The environment does not describe a runnable server."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    auth_token: str = ""
    heartbeat_interval_ms: int = DEFAULT_HEARTBEAT_INTERVAL_MS
    heartbeat_lease_ms: int = DEFAULT_HEARTBEAT_LEASE_MS
    handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S

    def __post_init__(self) -> None:
        if self.heartbeat_lease_ms <= self.heartbeat_interval_ms:
            raise ConfigError(
                f"heartbeat lease ({self.heartbeat_lease_ms}ms) must exceed the interval "
                f"({self.heartbeat_interval_ms}ms), or every body latches safe_hold "
                f"on its first heartbeat"
            )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ServerConfig:
        source = os.environ if env is None else env

        return cls(
            host=source.get("BRAIN_HOST", DEFAULT_HOST),
            port=_int(source, "BRAIN_PORT", DEFAULT_PORT),
            auth_token=source.get("BRAIN_AUTH_TOKEN", ""),
            heartbeat_interval_ms=_int(
                source, "BRAIN_HEARTBEAT_INTERVAL_MS", DEFAULT_HEARTBEAT_INTERVAL_MS
            ),
            heartbeat_lease_ms=_int(source, "BRAIN_HEARTBEAT_LEASE_MS", DEFAULT_HEARTBEAT_LEASE_MS),
        )


def _int(source: dict[str, str] | os._Environ[str], name: str, default: int) -> int:
    raw = source.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
