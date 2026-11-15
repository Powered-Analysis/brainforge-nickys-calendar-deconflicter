"""Issue #51: a database error while renewing one calendar becomes that calendar's failure
message, which names no calendar, and the renewal goes on to the next channel.

Google is replaced by the `httplib2`-compatible transport the renewal tests use, which answers
`events.watch` and `channels.stop`. The database is the libSQL container; a failing
`replace_channel` stands in for a database that errors on one calendar's save.
"""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from dispatcher import db, google_calendar, watches
from dispatcher.app import app
from dispatcher.google_calendar import GoogleCalendar
from test_calendar_attach import (  # noqa: F401 (fixtures)
    HOME,
    SIDE,
    WEBHOOK,
    WORK,
    database,
    rows,
    sentience,
    settings_env,
)
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


def fail_saves_for(monkeypatch, *calendar_ids):
    """Makes the channel save raise `db.Error` for these calendars, with their ID in the text."""
    real_replace = db.replace_channel

    def replace(calendar_id, *args):
        if calendar_id in calendar_ids:
            raise db.Error(f"{DATABASE_TEXT} on {calendar_id}")
        return real_replace(calendar_id, *args)

    monkeypatch.setattr(db, "replace_channel", replace)


def renew_all():
    return watches.renew_all(GoogleCalendar(), db.get_active_channels(), WEBHOOK)


def opened_for(google):
    """Maps each channel the fake Google opened to the calendar it was opened for."""
    return {
        json.loads(r["body"])["id"]: r["path"].split("/")[4] for r in google.watches
    }


def renewed(google):
    """The calendars whose row now records a channel that Google opened."""
    opened = opened_for(google)
    return {cal for cal, row in rows().items() if row[1] in opened}


# --- renew_all: the failure is recorded and the loop goes on ------------------------------


def test_a_database_error_on_one_calendar_is_returned_as_one_failure(
    google, monkeypatch
):
    seed(WORK, 1, 1)
    fail_saves_for(monkeypatch, WORK)
    failures = renew_all()
    assert len(failures) == 1


def test_the_failure_names_no_calendar_channel_or_database_text(google, monkeypatch):
    seed(WORK, 1, 1)
    fail_saves_for(monkeypatch, WORK)
    (message,) = renew_all()
    assert isinstance(message, str)
    assert message
    assert WORK not in message
    assert "old-channel-1" not in message
    assert "old-resource-1" not in message
    assert DATABASE_TEXT not in message
    (watch,) = google.watch_bodies()
    assert watch["id"] not in message


def test_the_calendars_after_the_failing_one_are_renewed(google, monkeypatch):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    seed(SIDE, 1, 3)
    fail_saves_for(monkeypatch, WORK)
    renew_all()
    assert renewed(google) == {HOME, SIDE}


def test_the_calendars_before_the_failing_one_stay_renewed(google, monkeypatch):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    seed(SIDE, 1, 3)
    fail_saves_for(monkeypatch, SIDE)
    failures = renew_all()
    assert len(failures) == 1
    assert renewed(google) == {WORK, HOME}


def test_a_failure_in_the_middle_still_renews_both_neighbors(google, monkeypatch):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    seed(SIDE, 1, 3)
    fail_saves_for(monkeypatch, HOME)
    renew_all()
    assert renewed(google) == {WORK, SIDE}


def test_each_failing_calendar_adds_its_own_failure(google, monkeypatch):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    seed(SIDE, 1, 3)
    fail_saves_for(monkeypatch, WORK, SIDE)
    failures = renew_all()
    assert len(failures) == 2
    assert renewed(google) == {HOME}
    for message in failures:
        assert WORK not in message
        assert SIDE not in message
        assert DATABASE_TEXT not in message


def test_a_database_error_does_not_escape_renew_all(google, monkeypatch):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    fail_saves_for(monkeypatch, WORK, HOME)
    assert len(renew_all()) == 2


def test_without_a_database_error_there_are_no_failures(google):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    assert renew_all() == []
    assert renewed(google) == {WORK, HOME}


def test_a_database_error_and_a_google_failure_are_each_reported(google, monkeypatch):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    seed(SIDE, 1, 3)
    fail_saves_for(monkeypatch, WORK)
    google.watch_failures[HOME] = 500
    failures = renew_all()
    assert len(failures) == 2
    assert renewed(google) == {SIDE}
    for message in failures:
        assert WORK not in message
        assert HOME not in message


# --- The failing calendar is left as it was, with no channel left open --------------------


def test_the_failing_calendars_row_keeps_its_old_channel(google, monkeypatch):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    before = rows()
    fail_saves_for(monkeypatch, WORK)
    renew_all()
    assert rows()[WORK] == before[WORK]


def test_the_failing_calendars_new_channel_is_stopped_and_its_old_one_is_not(
    google, monkeypatch
):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    fail_saves_for(monkeypatch, WORK)
    renew_all()
    opened = opened_for(google)
    work_channel = next(c for c, cal in opened.items() if cal == WORK)
    stopped = {b["id"] for b in google.stop_bodies()}
    assert work_channel in stopped
    assert "old-channel-1" not in stopped
    assert "old-channel-2" in stopped


# --- Through the renewal ping -------------------------------------------------------------


def test_the_ping_answers_502_when_a_database_error_failed_one_calendar(
    client, google, monkeypatch
):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    fail_saves_for(monkeypatch, WORK)
    response = client.post(URL, headers=AUTH)
    assert response.status_code == 502


def test_the_ping_renews_the_other_calendars_after_a_database_error(
    client, google, monkeypatch
):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    seed(SIDE, 1, 3)
    fail_saves_for(monkeypatch, WORK)
    client.post(URL, headers=AUTH)
    assert renewed(google) == {HOME, SIDE}
    assert rows()[WORK][1] == "old-channel-1"


def test_the_ping_log_and_response_name_no_calendar_or_database_text(
    client, google, monkeypatch, caplog
):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    fail_saves_for(monkeypatch, WORK)
    with caplog.at_level(logging.DEBUG):
        response = client.post(URL, headers=AUTH)
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and not r.name.startswith("httpx")
    ]
    assert warnings
    seen = "\n".join(warnings) + response.text
    assert WORK not in seen
    assert DATABASE_TEXT not in seen


def test_the_ping_answers_200_when_every_renewal_saves(client, google):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    assert client.post(URL, headers=AUTH).status_code == 200
    assert renewed(google) == {WORK, HOME}
