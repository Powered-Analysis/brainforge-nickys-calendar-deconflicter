"""Issue #56: when saving a renewed channel fails and stopping the new channel fails too, the
renewal path's failure message names no calendar and omits the database's text, because the
renewal workflow's log is public (Renewal Ping, `POST /internal/renew-watches`).

Google is replaced by the `httplib2`-compatible transport the renewal tests use. The database is
the libSQL container; the failing save stands in for a database whose error text names the
calendar.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from dispatcher import db, google_calendar, watches
from dispatcher.app import app
from dispatcher.google_calendar import GoogleCalendar
from test_calendar_attach import (  # noqa: F401 (fixtures)
    HOME,
    WEBHOOK,
    WORK,
    database,
    rows,
    sentience,
    settings_env,
)
from test_renew_save_failure import fail_every_stop_of_new_channels
from test_watch_renewal_and_detach import (  # noqa: F401 (fixtures)
    AUTH,
    URL,
    FakeGoogleWithStop,
    secret,
    seed,
)

DATABASE_TEXT = "fictional database outage"


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogleWithStop()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def fail_saves_for_work(monkeypatch, error_type):
    """Saving WORK's renewed channel raises an error whose text names the calendar."""
    real_replace = db.replace_channel

    def replace(calendar_id, *args):
        if calendar_id == WORK:
            raise error_type(f"{DATABASE_TEXT} while saving {calendar_id}")
        return real_replace(calendar_id, *args)

    monkeypatch.setattr(db, "replace_channel", replace)


def public_output(caplog, response=None):
    logged = "\n".join(
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and not r.name.startswith("httpx")
    )
    return logged + (response.text if response is not None else "")


@pytest.mark.parametrize("error_type", [db.Error, RuntimeError])
def test_the_ping_log_and_response_name_no_calendar_or_database_text(
    client, google, monkeypatch, caplog, error_type
):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    fail_saves_for_work(monkeypatch, error_type)
    fail_every_stop_of_new_channels(google, 500)
    with caplog.at_level(logging.DEBUG):
        response = client.post(URL, headers=AUTH)
    assert response.status_code == 502
    seen = public_output(caplog, response)
    assert seen.strip()
    assert WORK not in seen
    assert DATABASE_TEXT not in seen


def test_nothing_logged_at_any_level_carries_the_calendar_or_database_text(
    client, google, monkeypatch, caplog
):
    seed(WORK, 1, 1)
    fail_saves_for_work(monkeypatch, db.Error)
    fail_every_stop_of_new_channels(google, 500)
    with caplog.at_level(logging.DEBUG):
        client.post(URL, headers=AUTH)
    app_records = [r for r in caplog.records if not r.name.startswith("httpx")]
    assert app_records
    for record in app_records:
        text = record.getMessage() + (record.exc_text or "")
        assert WORK not in text
        assert DATABASE_TEXT not in text


def test_renew_all_returns_a_message_without_the_calendar_or_database_text(
    google, monkeypatch
):
    seed(WORK, 1, 1)
    fail_saves_for_work(monkeypatch, db.Error)
    fail_every_stop_of_new_channels(google, 500)
    failures = watches.renew_all(GoogleCalendar(), db.get_active_channels(), WEBHOOK)
    assert len(failures) == 1
    assert WORK not in failures[0]
    assert DATABASE_TEXT not in failures[0]


def test_the_message_still_says_the_stop_failed_and_why(google, monkeypatch):
    seed(WORK, 1, 1)
    fail_saves_for_work(monkeypatch, db.Error)
    fail_every_stop_of_new_channels(google, 500)
    (message,) = watches.renew_all(GoogleCalendar(), db.get_active_channels(), WEBHOOK)
    assert "500" in message
    assert "stop" in message.lower()


def test_the_other_calendars_are_still_renewed_after_the_double_failure(
    google, monkeypatch
):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    fail_saves_for_work(monkeypatch, db.Error)
    fail_every_stop_of_new_channels(google, 500)
    failures = watches.renew_all(GoogleCalendar(), db.get_active_channels(), WEBHOOK)
    assert len(failures) == 1
    after = rows()
    assert after[WORK][1] == "old-channel-1"
    assert after[HOME][1] != "old-channel-2"


def test_a_save_failure_whose_stop_succeeds_still_names_no_calendar(
    client, google, monkeypatch, caplog
):
    seed(WORK, 1, 1)
    fail_saves_for_work(monkeypatch, db.Error)
    with caplog.at_level(logging.DEBUG):
        response = client.post(URL, headers=AUTH)
    assert response.status_code == 502
    seen = public_output(caplog, response)
    assert WORK not in seen
    assert DATABASE_TEXT not in seen
