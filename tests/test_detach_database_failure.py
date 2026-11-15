"""Issue #54: in `calendar detach <id>`, a database failure is reported through the same path as
other operator errors: `watches.detach` raises `OperatorError`, and the command prints
`error: ...` to stderr, exits 1, and shows no traceback (convention 3: errors are raised and
reported the same way everywhere).

Google is replaced by the `httplib2`-compatible transport the detach tests use, with
`channels.stop`. Sentience must never be called. The database is the libSQL container; a
wrapped connection stands in for a database that fails on one kind of statement.
"""

# ruff: noqa: F811 (tests take the imported fixtures by name)

import pytest

from dispatcher import db, google_calendar, watches
from dispatcher.errors import OperatorError
from dispatcher.google_calendar import GoogleCalendar
from test_attach_all_database_failure import fail_reads_for
from test_calendar_attach import (  # noqa: F401 (fixtures)
    HOME,
    WORK,
    database,
    rows,
    sentience,
    settings_env,
)
from test_watch_renewal_and_detach import (  # noqa: F401 (fixtures)
    FakeGoogleWithStop,
    run,
    seed,
)

MESSAGE = "fictional connection reset"


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogleWithStop()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


@pytest.fixture
def two(google):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    return rows()


def fail_updates(monkeypatch, message=MESSAGE):
    """Makes every database write (UPDATE) raise `db.Error`, whichever `db` function asks."""
    real_connect = db.connect

    class Connection:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, parameters=()):
            if sql.lstrip().upper().startswith("UPDATE"):
                raise db.Error(message)
            return self.inner.execute(sql, parameters)

        def close(self):
            self.inner.close()

    monkeypatch.setattr(db, "connect", lambda: Connection(real_connect()))


# --- watches.detach -------------------------------------------------------------------------


def test_a_failing_read_is_raised_as_an_operator_error(google, two, monkeypatch):
    fail_reads_for(monkeypatch, WORK, MESSAGE)
    with pytest.raises(OperatorError) as raised:
        watches.detach(GoogleCalendar(), WORK)
    assert str(raised.value) == f"the database failed: {MESSAGE}"


def test_a_failing_write_is_raised_as_an_operator_error(google, two, monkeypatch):
    fail_updates(monkeypatch)
    with pytest.raises(OperatorError) as raised:
        watches.detach(GoogleCalendar(), WORK)
    assert str(raised.value) == f"the database failed: {MESSAGE}"


def test_a_failing_read_stops_nothing_at_google_and_leaves_the_calendar_active(
    google, two, monkeypatch
):
    fail_reads_for(monkeypatch, WORK, MESSAGE)
    with pytest.raises(OperatorError):
        watches.detach(GoogleCalendar(), WORK)
    assert google.stops == []
    assert rows() == two


def test_a_failing_write_leaves_the_calendar_active(google, two, monkeypatch):
    fail_updates(monkeypatch)
    with pytest.raises(OperatorError):
        watches.detach(GoogleCalendar(), WORK)
    assert rows() == two


def test_a_failing_write_leaves_no_other_calendar_changed(google, two, monkeypatch):
    fail_updates(monkeypatch)
    with pytest.raises(OperatorError):
        watches.detach(GoogleCalendar(), WORK)
    assert rows()[HOME] == two[HOME]


def test_a_database_failure_on_another_calendar_does_not_affect_this_one(
    google, two, monkeypatch
):
    fail_reads_for(monkeypatch, WORK, MESSAGE)
    watches.detach(GoogleCalendar(), HOME)
    assert rows()[HOME][5] == 0


def test_an_error_that_is_not_a_database_error_is_not_swallowed(
    google, two, monkeypatch
):
    class Unexpected(Exception):
        pass

    def read(*args, **kwargs):
        raise Unexpected("fictional programming error")

    monkeypatch.setattr(db, "get_active_channel", read)
    with pytest.raises(Unexpected):
        watches.detach(GoogleCalendar(), WORK)


def test_a_calendar_that_is_not_attached_is_still_reported_as_not_attached(google, two):
    with pytest.raises(OperatorError) as raised:
        watches.detach(GoogleCalendar(), "nobody@example.com")
    assert "the database failed" not in str(raised.value)
    assert "nobody@example.com" in str(raised.value)


def test_a_google_failure_is_still_reported_as_a_detach_failure_not_a_database_one(
    google, two
):
    google.stop_failures["old-channel-1"] = 500
    with pytest.raises(OperatorError) as raised:
        watches.detach(GoogleCalendar(), WORK)
    assert "the database failed" not in str(raised.value)
    assert rows() == two


def test_a_detach_that_fails_on_the_write_can_be_run_again(google, two, monkeypatch):
    with monkeypatch.context() as broken:
        fail_updates(broken)
        with pytest.raises(OperatorError):
            watches.detach(GoogleCalendar(), WORK)
    watches.detach(GoogleCalendar(), WORK)
    assert rows()[WORK][5] == 0


def test_a_detach_with_a_working_database_raises_nothing(google, two):
    watches.detach(GoogleCalendar(), WORK)
    assert rows()[WORK][5] == 0


# --- The command ----------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["read", "write"])
def test_the_command_prints_an_error_line_to_stderr_and_exits_1(
    google, two, run, monkeypatch, failure
):
    if failure == "read":
        fail_reads_for(monkeypatch, WORK, MESSAGE)
    else:
        fail_updates(monkeypatch)
    status, out, err = run(WORK)
    assert status == 1
    assert f"error: the database failed: {MESSAGE}" in err
    assert "detached" not in out


@pytest.mark.parametrize("failure", ["read", "write"])
def test_the_command_gives_no_traceback(google, two, run, monkeypatch, failure):
    if failure == "read":
        fail_reads_for(monkeypatch, WORK, MESSAGE)
    else:
        fail_updates(monkeypatch)
    _, out, err = run(WORK)
    assert "Traceback" not in out + err


def test_the_command_reports_one_error_line(google, two, run, monkeypatch):
    fail_updates(monkeypatch)
    _, _, err = run(WORK)
    assert [line for line in err.splitlines() if line.startswith("error")] == [
        f"error: the database failed: {MESSAGE}"
    ]


def test_the_database_failure_line_has_the_form_of_every_other_operator_error(
    google, two, run, monkeypatch
):
    _, _, not_attached = run("nobody@example.com")
    fail_updates(monkeypatch)
    status, _, failed = run(WORK)
    assert status == 1
    assert not_attached.startswith("error: ")
    assert failed.startswith("error: ")


def test_the_command_leaves_the_calendar_active_after_a_database_failure(
    google, two, run, monkeypatch
):
    fail_updates(monkeypatch)
    run(WORK)
    assert rows() == two


def test_the_command_never_talks_to_sentience(google, two, run, monkeypatch, sentience):
    fail_updates(monkeypatch)
    run(WORK)
    assert sentience == []


def test_an_unreachable_database_is_reported_without_a_traceback(
    google, two, run, monkeypatch
):
    # The setting is restored before the fixtures wipe the database, which needs it.
    with monkeypatch.context() as unreachable:
        unreachable.setenv("TURSO_DATABASE_URL", "http://127.0.0.1:1")
        status, out, err = run(WORK)
    assert status == 1
    assert err.startswith("error: the database failed:")
    assert "Traceback" not in out + err
