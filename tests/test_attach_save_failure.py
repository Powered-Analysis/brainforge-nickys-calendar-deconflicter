"""Issue #17: when saving an attached calendar fails after `events.watch` succeeded, the new
channel is closed through the Google Calendar module, so Google keeps no channel that no row records.

Google is replaced by the `httplib2`-compatible transport the attach tests use, extended with
`channels.stop`. Sentience is replaced by an `httpx2` transport that must never be called. The
database is the libSQL container; the failing save stands in for an unreachable database.
"""

import pytest

from dispatcher import attach, db, google_calendar
from dispatcher.google_calendar import GoogleCalendar
from test_calendar_attach import (  # noqa: F401 (fixtures)
    EXPIRATION_MS,
    HOME,
    WEBHOOK,
    WORK,
    database,
    entry,
    rows,
    sentience,
    settings_env,
)
from test_watch_renewal_and_detach import FakeGoogleWithStop

NOW = 1700000000


class DatabaseDown(Exception):
    pass


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogleWithStop()
    fake.add(entry(HOME, summary="Fictional Home"))
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


@pytest.fixture
def failing_save(monkeypatch):
    """Makes every save raise; returns the calls it saw."""
    calls = []

    def save(*args, **kwargs):
        calls.append(args)
        raise DatabaseDown("fictional database outage")

    monkeypatch.setattr(db, "save_attached_calendar", save)
    return calls


def attach_entry(google, calendar_id=WORK):
    session = GoogleCalendar()
    return attach._attach_entry(session, google.entries[calendar_id], WEBHOOK, NOW)


def test_a_failed_save_stops_the_channel_that_was_just_opened(google, failing_save):
    with pytest.raises(DatabaseDown):
        attach_entry(google)
    (watch,) = google.watch_bodies()
    (stop,) = google.stop_bodies()
    assert stop == {"id": watch["id"], "resourceId": f"resource-for-{WORK}"}


def test_the_save_error_itself_propagates_unchanged(google, failing_save):
    with pytest.raises(DatabaseDown, match="fictional database outage"):
        attach_entry(google)


def test_the_save_is_attempted_with_the_channel_that_is_then_stopped(
    google, failing_save
):
    with pytest.raises(DatabaseDown):
        attach_entry(google)
    (saved,) = failing_save
    (stop,) = google.stop_bodies()
    assert saved[:3] == (WORK, stop["id"], stop["resourceId"])
    assert saved[3] == EXPIRATION_MS


def test_the_channel_is_stopped_only_after_it_was_opened(google, failing_save):
    with pytest.raises(DatabaseDown):
        attach_entry(google)
    paths = [r["path"] for r in google.writes]
    assert paths == [
        f"/calendar/v3/calendars/{WORK}/events/watch",
        "/calendar/v3/channels/stop",
    ]


def test_a_failed_save_leaves_no_row(google, failing_save):
    with pytest.raises(DatabaseDown):
        attach_entry(google)
    assert rows() == {}


def test_a_stop_that_finds_the_channel_already_gone_still_raises_the_save_error(
    google, failing_save
):
    google.stop_failures = _fail_every_stop(google, 404)
    with pytest.raises(DatabaseDown):
        attach_entry(google)


def test_a_successful_save_stops_nothing(google):
    attach_entry(google)
    assert google.stops == []
    assert WORK in rows()


def test_a_failed_watch_stops_nothing_because_no_channel_exists(google, failing_save):
    google.watch_failures[WORK] = 500
    assert attach_entry(google).result == "failed"
    assert google.stops == []
    assert failing_save == []


def test_a_calendar_that_is_not_owned_stops_nothing(google, failing_save):
    google.entries[WORK] = entry(WORK, role="reader")
    assert attach_entry(google).result == "refused"
    assert google.writes == []


def test_attach_all_reports_a_calendar_whose_stop_failed_and_attaches_the_rest(
    google, monkeypatch
):
    real_save = db.save_attached_calendar

    def save(calendar_id, *args):
        if calendar_id == WORK:
            raise DatabaseDown("fictional database outage")
        return real_save(calendar_id, *args)

    monkeypatch.setattr(db, "save_attached_calendar", save)
    google.stop_failures = _fail_every_stop(google, 500)
    outcomes = {
        o.calendar_id: o.result
        for o in attach.attach_all(GoogleCalendar(), WEBHOOK, NOW)
    }
    assert outcomes == {WORK: "failed", HOME: "attached"}
    assert set(rows()) == {HOME}


def _fail_every_stop(google, status):
    """Queues a failure for the channel the next watch call opens, whatever its random ID."""

    class Everything(dict):
        def __contains__(self, key):
            return True

        def __getitem__(self, key):
            return status

    return Everything()


# Issue #40: when the save fails and the stop also fails, the operator must learn both
# failures from the reported detail.


def _failed_detail(google, status=500):
    google.stop_failures = _fail_every_stop(google, status)
    outcome = attach_entry(google)
    assert outcome.result == "failed"
    return outcome.detail


def test_a_failed_stop_after_a_failed_save_names_the_channel_that_was_left_open(
    google, failing_save
):
    detail = _failed_detail(google)
    (watch,) = google.watch_bodies()
    assert watch["id"] in detail


def test_a_failed_stop_after_a_failed_save_says_that_saving_failed(
    google, failing_save
):
    detail = _failed_detail(google)
    assert "fictional database outage" in detail
    assert "sav" in detail.lower()


def test_a_failed_stop_after_a_failed_save_still_says_why_the_stop_failed(
    google, failing_save
):
    assert "500" in _failed_detail(google, 500)


def test_attach_all_reports_both_failures_as_the_calendars_failed_detail(
    google, monkeypatch
):
    real_save = db.save_attached_calendar

    def save(calendar_id, *args):
        if calendar_id == WORK:
            raise DatabaseDown("fictional database outage")
        return real_save(calendar_id, *args)

    monkeypatch.setattr(db, "save_attached_calendar", save)
    google.stop_failures = _fail_every_stop(google, 500)
    outcomes = {
        o.calendar_id: o for o in attach.attach_all(GoogleCalendar(), WEBHOOK, NOW)
    }
    failed = outcomes[WORK]
    watch_ids = {w["id"] for w in google.watch_bodies()}
    assert failed.result == "failed"
    assert "fictional database outage" in failed.detail
    assert any(channel in failed.detail for channel in watch_ids)
    assert outcomes[HOME].result == "attached"


def test_attach_all_reports_only_the_save_failure_when_the_stop_succeeds(
    google, monkeypatch
):
    def save(*args, **kwargs):
        raise db.Error("fictional database outage")

    monkeypatch.setattr(db, "save_attached_calendar", save)
    outcomes = attach.attach_all(GoogleCalendar(), WEBHOOK, NOW)
    assert all(o.result == "failed" for o in outcomes)
    assert all("fictional database outage" in o.detail for o in outcomes)
    watch_ids = {w["id"] for w in google.watch_bodies()}
    assert not any(channel in o.detail for o in outcomes for channel in watch_ids)
    assert len(google.stop_bodies()) == len(outcomes)
