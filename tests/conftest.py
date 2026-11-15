import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The names the app may read come from the owner's `.env.example`, not from the code under test.
SETTING_NAMES = tuple(
    re.findall(
        r"^([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(), re.MULTILINE
    )
)


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch, tmp_path):
    """No setting leaks in from the machine or from a `.env` in the working directory."""
    database_url = os.environ["TURSO_DATABASE_URL"]
    for name in SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TURSO_DATABASE_URL", database_url)
    monkeypatch.chdir(tmp_path)
