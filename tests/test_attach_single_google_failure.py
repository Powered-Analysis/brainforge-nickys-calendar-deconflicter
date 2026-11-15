"""Issue #53: in `calendar attach <id>`, a Google failure is that calendar's `failed` line,
reported the same way as in `calendar attach all`: `failed <id>: <error>`, exit status 1,
no traceback (convention 3: errors are reported the same way everywhere).

Google is replaced by the `httplib2`-compatible transport the attach tests use. Sentience must
never be called. The database is the libSQL container.
"""

# ruff: noqa: F811 (tests take the imported fixtures by name)

import pytest

from dispatcher import attach
from dispatcher.google_calendar import GoogleCalendar
from test_calendar_attach import (  # noqa: F401 (fixtures)
    HOME,
    WEBHOOK,
    WORK,
    database,
    google,
    mixed_list,
    rows,
    run,
    sentience,
    settings_env,
)

NOW = 1700000000

FAILURES = pytest.mark.parametrize(
    "failure", ["watch_failures", "events_failures"], ids=["watch", "event-read"]
)


# --- The outcome of attaching one calendar ------------------------------------------------


@FAILURES
def test_a_google_failure_is_the_calendars_failed_outcome(google, failure):
    getattr(google, failure)[WORK] = 500
    outcome = attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    assert outcome.calendar_id == WORK
    assert outcome.result == "failed"
    assert outcome.detail
    assert "the database failed" not in outcome.detail


@FAILURES
def test_a_google_failure_stores_no_row(google, failure):
    getattr(google, failure)[WORK] = 503
    attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    assert rows() == {}


def test_a_failed_event_read_opens_no_channel(google):
    google.events_failures[WORK] = 503
    attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    assert google.watches == []


@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
def test_every_google_error_status_is_a_failed_outcome(google, status):
    google.watch_failures[WORK] = status
    outcome = attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    assert outcome.result == "failed"


def test_a_google_failure_is_not_raised_out_of_attach_one(google):
    google.watch_failures[WORK] = 500
    attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)


def test_single_and_all_give_the_same_outcome_for_the_same_failure(mixed_list):
    mixed_list.watch_failures[WORK] = 500
    single = attach.attach_one(GoogleCalendar(), WORK, WEBHOOK, NOW)
    by_all = {
        o.calendar_id: o for o in attach.attach_all(GoogleCalendar(), WEBHOOK, NOW)
    }
    assert single == by_all[WORK]


def test_a_google_failure_on_another_calendar_does_not_affect_this_one(mixed_list):
    mixed_list.watch_failures[WORK] = 500
    outcome = attach.attach_one(GoogleCalendar(), HOME, WEBHOOK, NOW)
    assert outcome.result == "attached"
    assert set(rows()) == {HOME}


# --- The command ---------------------------------------------------------------------------


@FAILURES
def test_the_command_prints_the_failed_line_to_stderr_and_exits_1(google, run, failure):
    getattr(google, failure)[WORK] = 500
    status, out, err = run(WORK)
    assert status == 1
    assert f"failed {WORK}: " in err
    assert WORK not in out


@FAILURES
def test_the_command_gives_no_error_prefix_and_no_traceback(google, run, failure):
    getattr(google, failure)[WORK] = 500
    _, out, err = run(WORK)
    assert "Traceback" not in out + err
    assert not any(line.startswith("error:") for line in (out + err).splitlines())


@FAILURES
def test_the_command_reports_one_failed_line_for_the_calendar(google, run, failure):
    getattr(google, failure)[WORK] = 500
    _, _, err = run(WORK)
    assert len([x for x in err.splitlines() if x.startswith("failed")]) == 1


@FAILURES
def test_the_command_stores_no_row_for_the_failed_calendar(google, run, failure):
    getattr(google, failure)[WORK] = 500
    run(WORK)
    assert rows() == {}


@FAILURES
def test_the_failure_line_is_the_one_attach_all_prints_for_the_same_failure(
    mixed_list, run, failure
):
    getattr(mixed_list, failure)[WORK] = 500
    single = run(WORK)
    by_all = run("all")
    assert single[0] == by_all[0] == 1
    single_line = f"failed {WORK}: "
    all_lines = [
        line for line in by_all[2].splitlines() if line.startswith(single_line)
    ]
    assert [
        line for line in single[2].splitlines() if line.startswith("failed")
    ] == all_lines


def test_a_google_failure_never_calls_sentience(google, run, sentience):
    google.watch_failures[WORK] = 500
    run(WORK)
    assert sentience == []


def test_the_command_still_succeeds_for_a_calendar_that_attaches(google, run):
    status, _, err = run(WORK)
    assert status == 0
    assert "failed" not in err
    assert set(rows()) == {WORK}
