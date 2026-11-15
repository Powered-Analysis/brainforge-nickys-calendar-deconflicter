"""The one place the app reads its settings. The names it may read are those in `.env.example`."""

import os
from pathlib import Path

from dispatcher.errors import OperatorError

# `.env.example` sits at the repository root, which the image also copies to /app.
ENV_EXAMPLE_FILE = Path(__file__).resolve().parents[2] / ".env.example"
ENV_FILE = Path(".env")


def _read_assignments(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


NAMES = tuple(_read_assignments(ENV_EXAMPLE_FILE))


def get(name: str) -> str | None:
    """The setting's value, or None when it is unset. A real environment variable wins over `.env`."""
    if name not in NAMES:
        raise KeyError(f"{name} is not a setting the app reads")
    if name in os.environ:
        return os.environ[name]
    if not ENV_FILE.is_file():
        return None
    return _read_assignments(ENV_FILE).get(name)


def require(name: str) -> str:
    value = get(name)
    if not value:
        raise OperatorError(f"{name} is not set")
    return value
