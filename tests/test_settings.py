import pytest

from conftest import SETTING_NAMES
from dispatcher import settings


def test_unset_setting_is_none():
    assert settings.get("RESEND_API_KEY") is None


def test_every_name_in_env_example_is_readable():
    for name in SETTING_NAMES:
        settings.get(name)


def test_environment_variable_is_read(monkeypatch):
    monkeypatch.setenv("ALERT_TO", "operator@example.com")
    assert settings.get("ALERT_TO") == "operator@example.com"


def test_env_file_is_read_when_no_environment_variable(tmp_path):
    (tmp_path / ".env").write_text("ALERT_TO=file@example.com\n")
    assert settings.get("ALERT_TO") == "file@example.com"


def test_environment_variable_wins_over_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ALERT_TO=file@example.com\n")
    monkeypatch.setenv("ALERT_TO", "env@example.com")
    assert settings.get("ALERT_TO") == "env@example.com"


def test_empty_environment_variable_still_wins_over_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("ALERT_TO=file@example.com\n")
    monkeypatch.setenv("ALERT_TO", "")
    assert not settings.get("ALERT_TO")


def test_env_file_only_supplies_the_names_it_holds(tmp_path):
    (tmp_path / ".env").write_text("ALERT_TO=file@example.com\n")
    assert settings.get("RESEND_API_KEY") is None


def test_a_name_outside_env_example_is_never_read(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("NOT_A_SETTING=leaked\n")
    monkeypatch.setenv("NOT_A_SETTING", "leaked")
    try:
        value = settings.get("NOT_A_SETTING")
    except KeyError, ValueError:
        return
    assert value is None


@pytest.mark.parametrize("name", ["ALERT_TO", "GOOGLE_WORKSPACE_DOMAIN"])
def test_names_are_independent(name, monkeypatch):
    monkeypatch.setenv(name, "example.com")
    others = [n for n in SETTING_NAMES if n != name and n != "TURSO_DATABASE_URL"]
    assert all(settings.get(n) is None for n in others)
