"""Issue #43: in `calendar attach <id>`, a database error is that calendar's `failed` line,
reported the same way as in `calendar attach all`: `failed <id>: the database failed: ...`,
exit status 1, no traceback (convention 3: errors are reported the same way everywhere).

Google is replaced by the `httplib2`-compatible transport the attach tests use, with
`channels.stop`. Sentience must never be called. The database is the libSQL container; a
patched `db` call stands in for an unreachable database.
"""

# ruff: noqa: F811 (tests take the imported fixtures by name)

import pytest

from dispatcher import attach, db, google_calendar
from dispatcher.google_calendar import GoogleCalendar
from test_attach_all_database_failure import fail_for, fail_reads_for
from test_calendar_attach import (  # noqa: F401 (fixtures)
    HOME,
    WEBHOOK,
    WORK,
    database,
    mixed_list,
    rows,
    run,
    sentience,
    settings_env,
)
from test_watch_renewal_and_detach import FakeGoogleWithStop

NOW = 1700000000
MESSAGE = "fictional connection reset"


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogleWithStop()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


def fail_every_save(monkeypatch, message=MESSAGE):
    def save(*args, **kwargs):
        raise db.Error(message)

    monkeypatch.setattr(db, "save_attached_calendar", save)


# --- The outcome of attaching one calendar ------------------------------------------------


def test_a_failing_save_is_the_calendars_failed_outcome(google, monkeypatch):
    fail_every_save(monkeypatch)
    outcome = attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    assert outcome.calendar_id == WORK
    assert outcome.result == "failed"
    assert outcome.detail == f"the database failed: {MESSAGE}"


def test_a_failing_read_is_the_calendars_failed_outcome(google, monkeypatch):
    fail_reads_for(monkeypatch, WORK, MESSAGE)
    outcome = attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    assert outcome.result == "failed"
    assert outcome.detail == f"the database failed: {MESSAGE}"
    assert rows() == {}


def test_a_failed_read_opens_no_channel_at_google(google, monkeypatch):
    fail_reads_for(monkeypatch, WORK, MESSAGE)
    attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    assert google.watches == []


def test_a_failed_save_leaves_no_channel_open_at_google(google, monkeypatch):
    fail_every_save(monkeypatch)
    attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    opened = {w["id"] for w in google.watch_bodies()}
    stopped = {s["id"] for s in google.stop_bodies()}
    assert len(opened) == 1
    assert stopped == opened
    assert rows() == {}


def test_a_database_error_is_not_raised_out_of_attach_one(google, monkeypatch):
    fail_every_save(monkeypatch)
    attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)


def test_a_failed_stop_after_a_failed_save_still_reports_both_failures_as_an_error(
    google, monkeypatch
):
    """The database failure is not the whole story when Google could not stop the channel, so
    the operator-facing error names both (a Google failure is not turned into a database one)."""
    fail_every_save(monkeypatch)
    google.stop_failures = _every_channel_fails()
    with pytest.raises(Exception) as raised:
        attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    assert MESSAGE in str(raised.value)
    assert "stopped" in str(raised.value)


def _every_channel_fails():
    class Always(dict):
        def __contains__(self, key):
            return True

        def __getitem__(self, key):
            return 500

    return Always()


def test_an_error_that_is_not_a_database_error_is_not_swallowed(google, monkeypatch):
    class Unexpected(Exception):
        pass

    def save(*args, **kwargs):
        raise Unexpected("fictional programming error")

    monkeypatch.setattr(db, "save_attached_calendar", save)
    with pytest.raises(Unexpected):
        attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)


def test_a_calendar_that_attaches_cleanly_reports_no_failure(google):
    outcome = attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    assert outcome.result == "attached"
    assert set(rows()) == {WORK}


def test_a_database_failure_on_another_calendar_does_not_affect_this_one(
    mixed_list, monkeypatch
):
    fail_for(monkeypatch, "save_attached_calendar", WORK)
    outcome = attach.attach_one(GoogleCalendar(), HOME, WEBHOOK, NOW)
    assert outcome.result == "attached"
    assert set(rows()) == {HOME}


# --- The command ---------------------------------------------------------------------------


def test_the_command_prints_the_failed_line_to_stderr_and_exits_1(
    google, run, monkeypatch
):
    fail_every_save(monkeypatch)
    status, out, err = run(WORK)
    assert status == 1
    assert f"failed {WORK}: the database failed: {MESSAGE}" in err
    assert WORK not in out


def test_the_command_reports_a_failing_read_the_same_way(google, run, monkeypatch):
    fail_reads_for(monkeypatch, WORK, MESSAGE)
    status, out, err = run(WORK)
    assert status == 1
    assert f"failed {WORK}: the database failed: {MESSAGE}" in err
    assert WORK not in out


def test_the_command_gives_no_traceback(google, run, monkeypatch):
    fail_every_save(monkeypatch)
    _, out, err = run(WORK)
    assert "Traceback" not in out + err


def test_the_command_stores_no_row_for_the_failed_calendar(google, run, monkeypatch):
    fail_every_save(monkeypatch)
    run(WORK)
    assert rows() == {}


def test_the_failure_line_is_the_one_attach_all_prints_for_the_same_failure(
    google, run, monkeypatch
):
    fail_every_save(monkeypatch)
    single = run(WORK)
    by_all = run("all")
    assert single[0] == by_all[0] == 1
    assert single[2] == by_all[2]


def test_the_command_reports_one_failed_line_for_the_calendar(google, run, monkeypatch):
    fail_every_save(monkeypatch)
    _, _, err = run(WORK)
    assert [line for line in err.splitlines() if line.startswith("failed")] == [
        f"failed {WORK}: the database failed: {MESSAGE}"
    ]


def test_a_google_failure_is_still_reported_as_an_error_not_a_database_failure(
    google, run
):
    google.watch_failures[WORK] = 500
    status, _, err = run(WORK)
    assert status == 1
    assert "the database failed" not in err
    assert "Traceback" not in err
