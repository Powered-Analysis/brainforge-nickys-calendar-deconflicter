"""Issue #41: when saving a renewed channel fails after `events.watch` succeeded, the new channel
is closed through the Google Calendar module, so Google keeps no channel that no row records.

Google is replaced by the `httplib2`-compatible transport the renewal tests use, which answers
`events.watch` and `channels.stop`. The database is the libSQL container; the failing save stands
in for an unreachable database.
"""

import json

import pytest
from fastapi.testclient import TestClient

from dispatcher import db, google_calendar, watches
from dispatcher.app import app
from dispatcher.google_calendar import GoogleCalendar, GoogleRequestFailed
from test_calendar_attach import (  # noqa: F401 (fixtures)
    EXPIRATION_MS,
    HOME,
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


class DatabaseDown(Exception):
    pass


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogleWithStop()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


@pytest.fixture
def failing_save(monkeypatch):
    """Makes every channel replacement raise; returns the calls it saw."""
    calls = []

    def replace(*args, **kwargs):
        calls.append(args)
        raise DatabaseDown("fictional database outage")

    monkeypatch.setattr(db, "replace_channel", replace)
    return calls


@pytest.fixture
def attached(google):
    seed(WORK, 1, 1)
    return rows()


def renew(calendar_id=WORK):
    channel = db.get_active_channel(calendar_id)
    return watches._renew(GoogleCalendar(), channel, WEBHOOK)


def fail_every_stop_of_new_channels(google, status):
    """Every channel except the seeded old ones fails to stop, whatever its random ID."""

    class NewChannels(dict):
        def __contains__(self, key):
            return not str(key).startswith("old-channel")

        def __getitem__(self, key):
            return status

    google.stop_failures = NewChannels()


def chain_of(error):
    links = []
    while error is not None:
        links.append(error)
        error = error.__cause__ or error.__context__
    return links


# --- The new channel is stopped and the save error propagates -----------------------------


def test_a_failed_save_stops_the_channel_that_was_just_opened(
    google, attached, failing_save
):
    with pytest.raises(DatabaseDown):
        renew()
    (watch,) = google.watch_bodies()
    assert {"id": watch["id"], "resourceId": f"resource-for-{WORK}"} in (
        google.stop_bodies()
    )


def test_the_save_error_itself_propagates_unchanged(google, attached, failing_save):
    with pytest.raises(DatabaseDown, match="fictional database outage"):
        renew()


def test_the_save_is_attempted_with_the_channel_that_is_then_stopped(
    google, attached, failing_save
):
    with pytest.raises(DatabaseDown):
        renew()
    (saved,) = failing_save
    (watch,) = google.watch_bodies()
    assert saved == (WORK, watch["id"], f"resource-for-{WORK}", EXPIRATION_MS)


def test_the_new_channel_is_stopped_only_after_it_was_opened(
    google, attached, failing_save
):
    with pytest.raises(DatabaseDown):
        renew()
    paths = [r["path"] for r in google.writes]
    assert paths == [
        f"/calendar/v3/calendars/{WORK}/events/watch",
        "/calendar/v3/channels/stop",
    ]


# --- The old channel stays recorded and live ----------------------------------------------


def test_a_failed_save_does_not_stop_the_old_channel(google, attached, failing_save):
    with pytest.raises(DatabaseDown):
        renew()
    assert "old-channel-1" not in {b["id"] for b in google.stop_bodies()}
    assert len(google.stops) == 1


def test_a_failed_save_leaves_the_calendars_row_as_it_was(
    google, attached, failing_save
):
    with pytest.raises(DatabaseDown):
        renew()
    assert rows() == attached


# --- A failed stop names both failures ----------------------------------------------------


def test_a_failed_stop_raises_google_request_failed_with_the_save_error_in_its_chain(
    google, attached, failing_save
):
    fail_every_stop_of_new_channels(google, 500)
    with pytest.raises(GoogleRequestFailed) as caught:
        renew()
    assert any(isinstance(link, DatabaseDown) for link in chain_of(caught.value))


def test_a_failed_stop_names_the_channel_that_was_left_open(
    google, attached, failing_save
):
    fail_every_stop_of_new_channels(google, 500)
    with pytest.raises(GoogleRequestFailed) as caught:
        renew()
    (watch,) = google.watch_bodies()
    assert watch["id"] in str(caught.value)


def test_a_failed_stop_says_that_saving_failed_and_why_the_stop_failed(
    google, attached, failing_save
):
    fail_every_stop_of_new_channels(google, 500)
    with pytest.raises(GoogleRequestFailed) as caught:
        renew()
    detail = str(caught.value)
    assert "fictional database outage" in detail
    assert "500" in detail


def test_a_stop_that_finds_the_channel_already_gone_still_raises_the_save_error(
    google, attached, failing_save
):
    fail_every_stop_of_new_channels(google, 404)
    with pytest.raises(DatabaseDown):
        renew()


# --- Cases that must not stop anything ----------------------------------------------------


def test_a_successful_save_stops_only_the_old_channel(google, attached):
    renew()
    assert [b["id"] for b in google.stop_bodies()] == ["old-channel-1"]
    (watch,) = google.watch_bodies()
    assert rows()[WORK][1] == watch["id"]


def test_a_failed_watch_stops_nothing_because_no_channel_exists(
    google, attached, failing_save
):
    google.watch_failures[WORK] = 500
    with pytest.raises(GoogleRequestFailed):
        renew()
    assert google.stops == []
    assert failing_save == []


def test_a_failed_old_channel_stop_after_a_good_save_does_not_stop_the_new_channel(
    google, attached
):
    google.stop_failures["old-channel-1"] = 500
    with pytest.raises(GoogleRequestFailed):
        renew()
    assert [b["id"] for b in google.stop_bodies()] == ["old-channel-1"]
    (watch,) = google.watch_bodies()
    assert rows()[WORK][1] == watch["id"]


# --- Through the renewal ping ------------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_the_ping_stops_the_new_channel_of_a_calendar_whose_save_failed_and_renews_the_rest(
    client, google, monkeypatch
):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    real_replace = db.replace_channel

    def replace(calendar_id, *args):
        if calendar_id == WORK:
            raise db.Error("fictional database outage")
        return real_replace(calendar_id, *args)

    monkeypatch.setattr(db, "replace_channel", replace)
    fail_every_stop_of_new_channels(google, 500)
    # A save error whose stop also failed is a Google failure the ping already handles.
    response = client.post(URL, headers=AUTH)
    assert response.status_code == 502
    after = rows()
    assert after[WORK][1] == "old-channel-1"
    opened = {
        json.loads(r["body"])["id"]: r["path"].split("/")[4] for r in google.watches
    }
    assert after[HOME][1] in opened
    assert opened[after[HOME][1]] == HOME


def test_the_ping_response_names_neither_the_channel_nor_the_save_error(
    client, google, monkeypatch
):
    seed(WORK, 1, 1)

    def replace(*args, **kwargs):
        raise db.Error("fictional database outage")

    monkeypatch.setattr(db, "replace_channel", replace)
    fail_every_stop_of_new_channels(google, 500)
    response = client.post(URL, headers=AUTH)
    assert response.status_code == 502
    (watch,) = google.watch_bodies()
    assert watch["id"] not in response.text
    assert "fictional database outage" not in response.text
