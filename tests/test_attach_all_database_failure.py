"""Issue #18: in `calendar attach all`, a database error on one calendar is that calendar's
`failed` line, the rest still attach, and the command exits nonzero (issue #2, criterion 7).

Google is replaced by the `httplib2`-compatible transport the attach tests use, with
`channels.stop`. Sentience must never be called. The database is the libSQL container; a
patched `db` call stands in for an unreachable database.
"""

# ruff: noqa: F811 (tests take the imported fixtures by name)

import pytest

from dispatcher import attach, db, google_calendar
from dispatcher.google_calendar import GoogleCalendar
from test_calendar_attach import (  # noqa: F401 (fixtures)
    HOME,
    SIDE,
    WEBHOOK,
    WORK,
    calendar_of,
    database,
    mixed_list,
    rows,
    run,
    sentience,
    settings_env,
)
from test_watch_renewal_and_detach import FakeGoogleWithStop

NOW = 1700000000
ALL_FORMS = pytest.mark.parametrize("arguments", [("all",), ("--auto-discover",)])


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogleWithStop()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


def fail_for(monkeypatch, name, calendar_id, message="fictional connection reset"):
    """Makes the named `db` function raise `db.Error` for one calendar and work for the rest."""
    real = getattr(db, name)

    def wrapper(given_id, *args, **kwargs):
        if given_id == calendar_id:
            raise db.Error(message)
        return real(given_id, *args, **kwargs)

    monkeypatch.setattr(db, name, wrapper)


def fail_reads_for(monkeypatch, calendar_id, message="fictional connection reset"):
    """Makes every database read about one calendar raise `db.Error`, whichever `db` function
    asks, so the test names no internal lookup."""
    real_connect = db.connect

    class Connection:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, parameters=()):
            if sql.lstrip().upper().startswith("SELECT") and calendar_id in parameters:
                raise db.Error(message)
            return self.inner.execute(sql, parameters)

        def close(self):
            self.inner.close()

    monkeypatch.setattr(db, "connect", lambda: Connection(real_connect()))


def outcomes_by_calendar():
    return {o.calendar_id: o for o in attach.attach_all(GoogleCalendar(), WEBHOOK, NOW)}


def test_the_database_error_class_is_an_exception_subclass():
    assert issubclass(db.Error, Exception)


def test_a_failing_save_is_that_calendars_failed_outcome(mixed_list, monkeypatch):
    fail_for(monkeypatch, "save_attached_calendar", WORK)
    outcomes = outcomes_by_calendar()
    assert outcomes[WORK].result == "failed"
    assert "fictional connection reset" in outcomes[WORK].detail
    assert "database" in outcomes[WORK].detail


def test_a_failing_save_does_not_stop_the_rest_attaching(mixed_list, monkeypatch):
    fail_for(monkeypatch, "save_attached_calendar", WORK)
    outcomes = outcomes_by_calendar()
    assert outcomes[HOME].result == "attached"
    assert outcomes[SIDE].result == "attached"
    assert set(rows()) == {HOME, SIDE}


def test_a_failing_save_on_the_last_calendar_is_still_reported(mixed_list, monkeypatch):
    fail_for(monkeypatch, "save_attached_calendar", SIDE)
    outcomes = outcomes_by_calendar()
    assert outcomes[SIDE].result == "failed"
    assert set(rows()) == {WORK, HOME}


def test_a_failing_save_on_the_first_calendar_is_still_reported(
    mixed_list, monkeypatch
):
    fail_for(monkeypatch, "save_attached_calendar", WORK)
    outcomes = list(outcomes_by_calendar().values())
    assert outcomes[0].calendar_id == WORK
    assert outcomes[0].result == "failed"


def test_every_calendar_failing_is_reported_once_each(mixed_list, monkeypatch):
    def save(*args, **kwargs):
        raise db.Error("fictional outage")

    monkeypatch.setattr(db, "save_attached_calendar", save)
    outcomes = attach.attach_all(GoogleCalendar(), WEBHOOK, NOW)
    assert sorted(o.calendar_id for o in outcomes) == sorted([WORK, HOME, SIDE])
    assert {o.result for o in outcomes} == {"failed"}
    assert rows() == {}


def test_a_database_error_while_checking_whether_a_calendar_is_attached_is_reported(
    mixed_list, monkeypatch
):
    fail_reads_for(monkeypatch, HOME)
    outcomes = outcomes_by_calendar()
    assert outcomes[HOME].result == "failed"
    assert outcomes[WORK].result == "attached"
    assert outcomes[SIDE].result == "attached"
    assert set(rows()) == {WORK, SIDE}


def test_a_failed_save_leaves_no_channel_open_at_google(mixed_list, monkeypatch):
    fail_for(monkeypatch, "save_attached_calendar", WORK)
    attach.attach_all(GoogleCalendar(), WEBHOOK, NOW)
    opened = {w["id"] for w in mixed_list.watch_bodies()}
    stopped = {s["id"] for s in mixed_list.stop_bodies()}
    assert len(opened) == 3
    assert len(stopped) == 1
    assert stopped <= opened


def test_an_error_that_is_not_a_database_error_is_not_swallowed(
    mixed_list, monkeypatch
):
    class Unexpected(Exception):
        pass

    def save(*args, **kwargs):
        raise Unexpected("fictional programming error")

    monkeypatch.setattr(db, "save_attached_calendar", save)
    with pytest.raises(Unexpected):
        attach.attach_all(GoogleCalendar(), WEBHOOK, NOW)


def test_calendars_that_attach_cleanly_report_no_failure(mixed_list):
    outcomes = outcomes_by_calendar()
    assert {o.result for o in outcomes.values()} == {"attached"}
    assert set(rows()) == {WORK, HOME, SIDE}


@ALL_FORMS
def test_the_command_prints_the_failed_line_to_stderr_and_exits_1(
    mixed_list, run, monkeypatch, arguments
):
    fail_for(monkeypatch, "save_attached_calendar", WORK)
    status, out, err = run(*arguments)
    assert status == 1
    assert f"failed {WORK}: the database failed: fictional connection reset" in err
    assert WORK not in out


@ALL_FORMS
def test_the_command_still_reports_the_calendars_that_attached(
    mixed_list, run, monkeypatch, arguments
):
    fail_for(monkeypatch, "save_attached_calendar", WORK)
    status, out, _ = run(*arguments)
    assert status == 1
    assert f"attached {HOME}" in out
    assert f"attached {SIDE}" in out
    assert set(rows()) == {HOME, SIDE}


@ALL_FORMS
def test_the_command_gives_no_traceback(mixed_list, run, monkeypatch, arguments):
    fail_for(monkeypatch, "save_attached_calendar", WORK)
    _, out, err = run(*arguments)
    assert "Traceback" not in out + err


def test_all_and_auto_discover_report_a_database_failure_identically(
    mixed_list, run, monkeypatch
):
    fail_for(monkeypatch, "save_attached_calendar", WORK)
    by_all = run("all")
    wipe_rows()
    mixed_list.writes.clear()
    by_flag = run("--auto-discover")
    assert by_all[0] == by_flag[0] == 1
    assert by_all[2] == by_flag[2]


def wipe_rows():
    conn = db.connect()
    try:
        conn.execute("DELETE FROM calendars")
    finally:
        conn.close()


def test_a_failed_database_call_writes_nothing_to_google_but_watch_and_stop(
    mixed_list, run, monkeypatch
):
    fail_for(monkeypatch, "save_attached_calendar", WORK)
    run("all")
    paths = {w["path"] for w in mixed_list.writes}
    assert paths == {
        f"/calendar/v3/calendars/{c}/events/watch" for c in (WORK, HOME, SIDE)
    } | {"/calendar/v3/channels/stop"}
    assert {calendar_of(w) for w in mixed_list.watches} == {WORK, HOME, SIDE}
