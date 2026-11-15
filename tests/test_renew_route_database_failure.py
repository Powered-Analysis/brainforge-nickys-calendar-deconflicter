"""Issue #57: a database error while the renewal route reads the expired-auth signal or the
active channels is reported as a renewal failure that names no calendar, not an unhandled 500
(Renewal Ping Authentication; The standing signal; the renewal workflow's log is public).

Google is replaced by the `httplib2`-compatible transport the renewal tests use. The database is
the libSQL container; a failing read stands in for a database that errors, with text that names
a calendar.
"""

import logging
import time

import pytest
from fastapi.testclient import TestClient

from dispatcher import db, google_calendar
from dispatcher.app import app
from test_calendar_attach import (  # noqa: F401 (fixtures)
    HOME,
    WORK,
    database,
    execute,
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
FAILING_READS = ["is_sentience_auth_expired", "get_active_channels"]


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogleWithStop()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


@pytest.fixture(autouse=True)
def clean_auth_state():
    """The shared wipe leaves the expired-auth marker, so each test starts and ends without it."""
    execute("DELETE FROM sentience_auth_expired")
    yield
    execute("DELETE FROM sentience_auth_expired")


@pytest.fixture
def client():
    # The route's database errors are reported, never raised into the test client.
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


def fail(monkeypatch, name):
    def failing(*args, **kwargs):
        raise db.Error(f"{DATABASE_TEXT} on {WORK}")

    monkeypatch.setattr(db, name, failing)


def public_output(caplog, response):
    logged = "\n".join(
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and not r.name.startswith("httpx")
    )
    return logged, logged + response.text


@pytest.mark.parametrize("name", FAILING_READS)
def test_a_database_error_is_answered_502_not_an_unhandled_error(
    client, google, monkeypatch, name
):
    seed(WORK, 1, 1)
    fail(monkeypatch, name)
    assert client.post(URL, headers=AUTH).status_code == 502


@pytest.mark.parametrize("name", FAILING_READS)
def test_the_log_reports_a_failure_and_names_no_calendar_or_database_text(
    client, google, monkeypatch, caplog, name
):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    fail(monkeypatch, name)
    with caplog.at_level(logging.DEBUG):
        response = client.post(URL, headers=AUTH)
    logged, seen = public_output(caplog, response)
    assert logged.strip()
    assert WORK not in seen
    assert HOME not in seen
    assert DATABASE_TEXT not in seen
    for record in caplog.records:
        if not record.name.startswith("httpx"):
            text = record.getMessage() + (record.exc_text or "")
            assert WORK not in text
            assert DATABASE_TEXT not in text


@pytest.mark.parametrize("name", FAILING_READS)
def test_the_calendar_rows_are_left_as_they_were(client, google, monkeypatch, name):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    before = rows()
    fail(monkeypatch, name)
    client.post(URL, headers=AUTH)
    assert rows() == before


def test_a_failed_channel_read_renews_nothing_at_google(client, google, monkeypatch):
    seed(WORK, 1, 1)
    fail(monkeypatch, "get_active_channels")
    client.post(URL, headers=AUTH)
    assert google.watches == []


def test_while_auth_is_expired_a_channel_read_error_still_answers_503(
    client, google, monkeypatch
):
    seed(WORK, 1, 1)
    db.mark_sentience_auth_expired(int(time.time()))
    fail(monkeypatch, "get_active_channels")
    assert client.post(URL, headers=AUTH).status_code == 503


def test_a_wrong_token_is_still_401_when_the_database_fails(
    client, google, monkeypatch
):
    seed(WORK, 1, 1)
    fail(monkeypatch, "is_sentience_auth_expired")
    fail(monkeypatch, "get_active_channels")
    response = client.post(URL, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_without_a_database_error_the_ping_answers_200(client, google):
    seed(WORK, 1, 1)
    assert client.post(URL, headers=AUTH).status_code == 200
