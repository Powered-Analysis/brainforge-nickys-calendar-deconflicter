"""The settings names come from `.env.example`: the file the owner provisions is the one list."""

import shutil
import subprocess
import sys

import pytest

from conftest import ROOT, SETTING_NAMES
from dispatcher import settings

PROBE = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from dispatcher import settings; print(','.join(settings.NAMES))"
)


def _copy_app(tmp_path, env_example):
    """A copy of the app laid out like the repository (or the image): `src/dispatcher` and the file."""
    root = tmp_path / "copy"
    shutil.copytree(
        ROOT / "src" / "dispatcher",
        root / "src" / "dispatcher",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    if env_example is not None:
        (root / ".env.example").write_text(env_example)
    return root


def _names_in_copy(root):
    return subprocess.run(
        [sys.executable, "-I", "-c", PROBE, str(root / "src")],
        check=False,
        capture_output=True,
        text=True,
        cwd=root,
    )


def test_names_are_exactly_the_assignments_in_env_example():
    assert set(settings.NAMES) == set(SETTING_NAMES)


def test_names_have_no_duplicates():
    assert len(settings.NAMES) == len(set(settings.NAMES))


def test_names_include_settings_that_ship_with_a_default_value():
    assert "SENTIENCE_MCP_URL" in settings.NAMES
    assert "SENTIENCE_AUTH_ISSUER" in settings.NAMES


def test_a_commented_out_assignment_is_not_a_name(tmp_path):
    root = _copy_app(tmp_path, "# OLD_SETTING=1\nKEPT_SETTING=\n")
    result = _names_in_copy(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split(",") == ["KEPT_SETTING"]


@pytest.mark.parametrize("name", SETTING_NAMES)
def test_every_listed_name_is_readable_from_the_environment(name, monkeypatch):
    monkeypatch.setenv(name, "value-from-environment")
    assert settings.get(name) == "value-from-environment"


def test_a_name_added_to_env_example_is_a_name_the_app_reads(tmp_path):
    root = _copy_app(tmp_path, "# header\nFIRST_SETTING=\nSECOND_SETTING=a-default\n")
    result = _names_in_copy(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split(",") == ["FIRST_SETTING", "SECOND_SETTING"]


def test_a_name_removed_from_env_example_is_no_longer_read(tmp_path):
    text = (ROOT / ".env.example").read_text().replace("ALERT_TO=\n", "")
    root = _copy_app(tmp_path, text)
    result = _names_in_copy(root)
    assert result.returncode == 0, result.stderr
    names = result.stdout.strip().split(",")
    assert "ALERT_TO" not in names
    assert "RESEND_API_KEY" in names


def test_a_missing_env_example_is_an_error_not_an_empty_list(tmp_path):
    root = _copy_app(tmp_path, None)
    result = _names_in_copy(root)
    assert result.returncode != 0


def test_the_image_copies_env_example_beside_the_installed_app():
    dockerfile = (ROOT / "Dockerfile").read_text()
    copies = [
        line.split()
        for line in dockerfile.splitlines()
        if line.startswith("COPY ") and ".env.example" in line
    ]
    assert copies, "the image never copies .env.example"
    assert all(parts[-1] in ("./", "/app/", "/app/.env.example") for parts in copies)
    assert "WORKDIR /app" in dockerfile
