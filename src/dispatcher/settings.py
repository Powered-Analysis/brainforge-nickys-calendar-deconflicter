"""The one place the app reads its settings: the names listed in `.env.example`."""

import os
from pathlib import Path

NAMES = (
    "GCP_CLIENT_ID",
    "GCP_CLIENT_SECRET",
    "GCP_REFRESH_TOKEN",
    "GOOGLE_WORKSPACE_DOMAIN",
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
    "PUBLIC_BASE_URL",
    "INTERNAL_CRON_SECRET",
    "RESEND_API_KEY",
    "ALERT_TO",
    "SENTIENCE_MCP_URL",
    "SENTIENCE_AUTH_ISSUER",
)

ENV_FILE = Path(".env")


def _read_env_file() -> dict[str, str]:
    if not ENV_FILE.is_file():
        return {}
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def get(name: str) -> str | None:
    """The setting's value, or None when it is unset. A real environment variable wins over `.env`."""
    if name not in NAMES:
        raise KeyError(f"{name} is not a setting the app reads")
    if name in os.environ:
        return os.environ[name]
    return _read_env_file().get(name)
