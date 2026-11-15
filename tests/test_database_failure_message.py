"""Issue #61: the wording of "the database failed" has one definition, `db.failure_message`, and
both places that report a database failure use it: a failed `attach` outcome and the
`OperatorError` raised by `watches.detach` (conventions 1 and 3).

Google is replaced by the `httplib2`-compatible transport the attach and detach tests use. The
database is the libSQL container; a patched `db` call stands in for a failing database.
"""

# ruff: noqa: F811 (tests take the imported fixtures by name)

import pytest

from dispatcher import attach, db, google_calendar, watches
from dispatcher.errors import OperatorError
from dispatcher.google_calendar import GoogleCalendar
from test_attach_all_database_failure import fail_reads_for
from test_attach_single_database_failure import fail_every_save
from test_calendar_attach import (  # noqa: F401 (fixtures)
    HOME,
    WEBHOOK,
    WORK,
    database,
    rows,
    sentience,
    settings_env,
)
from test_detach_database_failure import fail_updates
from test_watch_renewal_and_detach import FakeGoogleWithStop, seed

NOW = 1700000000
MESSAGE = "fictional connection reset"
SHARED = "fictional shared wording"


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogleWithStop()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


@pytest.fixture
def attached(google):
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)


def attach_detail(monkeypatch, calendar_id=WORK):
    fail_every_save(monkeypatch, MESSAGE)
    outcome = attach.attach_one(GoogleCalendar(), calendar_id, WEBHOOK, NOW)
    assert outcome.result == "failed"
    return outcome.detail


def detach_message(monkeypatch, calendar_id=WORK):
    fail_updates(monkeypatch, MESSAGE)
    with pytest.raises(OperatorError) as raised:
        watches.detach(GoogleCalendar(), calendar_id)
    return str(raised.value)


# --- The shared definition ------------------------------------------------------------------


def test_the_shared_definition_names_the_failure_and_carries_the_error_text():
    assert db.failure_message(db.Error(MESSAGE)) == f"the database failed: {MESSAGE}"


def test_the_shared_definition_works_for_a_real_database_error(database):
    with pytest.raises(db.Error) as raised:
        db.connect().execute("select * from no_such_table")
    assert db.failure_message(raised.value) == f"the database failed: {raised.value}"


# --- Both reports use it --------------------------------------------------------------------


def test_the_attach_outcome_uses_the_shared_definition(google, monkeypatch):
    monkeypatch.setattr(db, "failure_message", lambda error: SHARED)
    assert attach_detail(monkeypatch) == SHARED


def test_the_detach_error_uses_the_shared_definition(attached, monkeypatch):
    monkeypatch.setattr(db, "failure_message", lambda error: SHARED)
    assert detach_message(monkeypatch) == SHARED


def test_the_shared_definition_receives_the_database_error_from_attach(
    google, monkeypatch
):
    seen = []
    monkeypatch.setattr(
        db, "failure_message", lambda error: seen.append(error) or SHARED
    )
    attach_detail(monkeypatch)
    assert [str(error) for error in seen] == [MESSAGE]


def test_the_shared_definition_receives_the_database_error_from_detach(
    attached, monkeypatch
):
    seen = []
    monkeypatch.setattr(
        db, "failure_message", lambda error: seen.append(error) or SHARED
    )
    detach_message(monkeypatch)
    assert [str(error) for error in seen] == [MESSAGE]


def test_a_failing_read_in_attach_also_uses_the_shared_definition(google, monkeypatch):
    monkeypatch.setattr(db, "failure_message", lambda error: SHARED)
    fail_reads_for(monkeypatch, WORK, MESSAGE)
    outcome = attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    assert outcome.detail == SHARED


def test_a_failing_read_in_detach_also_uses_the_shared_definition(
    attached, monkeypatch
):
    monkeypatch.setattr(db, "failure_message", lambda error: SHARED)
    fail_reads_for(monkeypatch, WORK, MESSAGE)
    with pytest.raises(OperatorError) as raised:
        watches.detach(GoogleCalendar(), WORK)
    assert str(raised.value) == SHARED


def test_attach_and_detach_report_the_same_database_failure_in_the_same_words(
    google, monkeypatch
):
    seed(HOME, 1, 2)
    assert attach_detail(monkeypatch) == detach_message(monkeypatch, HOME)
