"""Issue #22: `calendar attach` decides whether a calendar is already attached through
`db.get_active_channel`, and `db.is_calendar_active` no longer exists (convention 3).

Google is replaced by the `httplib2`-compatible transport the attach tests use; Sentience must
never be called. The database is the libSQL container.
"""

# ruff: noqa: F811 (tests take the imported fixtures by name)

import pytest

from dispatcher import db
from test_calendar_attach import (  # noqa: F401 (fixtures)
    HOME,
    WORK,
    database,
    entry,
    google,
    rows,
    run,
    seed,
    sentience,
    settings_env,
)


@pytest.fixture
def asked(monkeypatch):
    """Records every calendar id passed to `db.get_active_channel`, then answers as it really does."""
    calls = []
    real = db.get_active_channel

    def spy(calendar_id):
        calls.append(calendar_id)
        return real(calendar_id)

    monkeypatch.setattr(db, "get_active_channel", spy)
    return calls


# --- Criterion 1: attach asks db.get_active_channel -------------------------------------------


def test_attach_asks_get_active_channel_about_the_calendar_being_attached(
    google, run, asked
):
    status, _, _ = run(WORK)
    assert status == 0
    assert WORK in asked


def test_a_calendar_get_active_channel_reports_attached_is_skipped(
    google, run, monkeypatch
):
    """The database is empty, so only the patched answer can make attach skip the calendar."""
    monkeypatch.setattr(
        db, "get_active_channel", lambda calendar_id: db.Channel(calendar_id, "c", "r")
    )
    status, out, err = run(WORK)
    assert status == 0
    assert "already" in (out + err).lower()
    assert google.watches == []
    assert rows() == {}


def test_a_calendar_get_active_channel_reports_unattached_is_attached(
    google, run, monkeypatch
):
    """The database says the calendar is active, so only the patched answer can make attach go on."""
    seed(WORK, active=1)
    monkeypatch.setattr(db, "get_active_channel", lambda calendar_id: None)
    status, _, _ = run(WORK)
    assert status == 0
    assert len(google.watches) == 1


# --- Criterion 2: the duplicate lookup is gone and attach behaves as before -------------------


def test_db_has_no_is_calendar_active():
    assert not hasattr(db, "is_calendar_active")


def test_an_active_calendar_is_still_skipped_and_left_alone(google, run, asked):
    before = seed(WORK, active=1)
    status, out, err = run(WORK)
    assert status == 0
    assert "already" in (out + err).lower()
    assert rows()[WORK] == before
    assert google.watches == []
    assert WORK in asked


def test_an_inactive_calendar_is_still_attached_again(google, run, asked):
    seed(WORK, active=0)
    status, _, _ = run(WORK)
    assert status == 0
    assert rows()[WORK][5] == 1
    assert rows()[WORK][1] != "old-channel"
    assert len(google.watches) == 1


def test_a_calendar_missing_from_the_database_is_still_attached(google, run, asked):
    google.add(entry(HOME))
    status, _, _ = run(HOME)
    assert status == 0
    assert HOME in rows()
    assert len(google.watches) == 1


def test_an_inactive_calendar_reads_as_not_attached(asked):
    seed(WORK, active=0)
    assert db.get_active_channel(WORK) is None


def test_an_active_calendar_reads_as_attached():
    seed(WORK, active=1)
    channel = db.get_active_channel(WORK)
    assert channel is not None
    assert channel.calendar_id == WORK
