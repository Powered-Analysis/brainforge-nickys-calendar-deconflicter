"""Issue #25: the rule that a span already over raises no conflict is defined once.

The expectations come from the issue: `conflicts` owns the past-overlap rule, a span ending at
or before `now` is over, and `ingress` uses that same rule for the incoming meeting. Behavior
is otherwise unchanged. Everything here is I/O-free: the current time is an argument.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from dispatcher import conflicts

DOMAIN = "example.com"
ZONE = "America/New_York"
END = datetime(2026, 3, 2, 11, 0, tzinfo=UTC)


def timed(event_id, start, end):
    return {
        "id": event_id,
        "iCalUID": f"{event_id}@google.com",
        "status": "confirmed",
        "start": {"dateTime": f"2026-03-02T{start}:00Z"},
        "end": {"dateTime": f"2026-03-02T{end}:00Z"},
        "organizer": {"email": "operator@example.com"},
        "attendees": [{"email": "operator@example.com"}],
    }


def meeting(raw):
    result = conflicts.meeting_from_event(
        "operator@example.com", raw, DOMAIN, lambda: ZONE
    )
    assert result is not None
    return result


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (END - timedelta(days=1), False),
        (END - timedelta(seconds=1), False),
        (END - timedelta(microseconds=1), False),
        (END, True),
        (END + timedelta(microseconds=1), True),
        (END + timedelta(seconds=1), True),
        (END + timedelta(days=1), True),
    ],
    ids=[
        "a_day_before",
        "a_second_before",
        "a_microsecond_before",
        "exactly_at_the_end",
        "a_microsecond_after",
        "a_second_after",
        "a_day_after",
    ],
)
def test_a_span_is_over_once_now_reaches_its_end(now, expected):
    assert conflicts.has_ended(END, now) is expected


def test_the_rule_compares_instants_not_wall_clock_times_across_zones():
    # 11:00 UTC is 06:00 in New York (EST, UTC-5): the same instant.
    new_york = timezone(timedelta(hours=-5))
    assert conflicts.has_ended(END, datetime(2026, 3, 2, 6, 0, tzinfo=new_york))
    assert not conflicts.has_ended(
        END, datetime(2026, 3, 2, 5, 59, 59, tzinfo=new_york)
    )
    # Wall clock 11:00 in New York is 16:00 UTC, five hours after END.
    assert conflicts.has_ended(END, datetime(2026, 3, 2, 11, 0, tzinfo=new_york))


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(hours=-1),
        timedelta(minutes=-1),
        timedelta(seconds=-1),
        timedelta(0),
        timedelta(seconds=1),
        timedelta(minutes=1),
        timedelta(hours=1),
    ],
    ids=[
        "an_hour_before",
        "a_minute_before",
        "a_second_before",
        "exactly_at",
        "a_second_after",
        "a_minute_after",
        "an_hour_after",
    ],
)
def test_a_conflict_is_raised_exactly_when_its_overlap_has_not_ended(offset):
    # The overlap of 10:00-11:00 and 10:30-11:30 runs to 11:00.
    first = meeting(timed("a", "10:00", "11:00"))
    other = meeting(timed("b", "10:30", "11:30"))
    now = END + offset
    found = conflicts.find_conflicts(first, [other], now)
    assert bool(found) is (not conflicts.has_ended(END, now))


def test_the_overlap_end_not_either_meetings_end_decides_whether_it_is_over():
    # `a` runs to 12:00 and `b` to 11:00, so the overlap ends with `b` at 11:00.
    first = meeting(timed("a", "10:00", "12:00"))
    other = meeting(timed("b", "10:30", "11:00"))
    assert conflicts.find_conflicts(first, [other], END) == []
    assert (
        len(conflicts.find_conflicts(first, [other], END - timedelta(seconds=1))) == 1
    )
    # The later-ending meeting alone being unfinished does not revive the conflict.
    assert conflicts.find_conflicts(first, [other], END + timedelta(minutes=30)) == []
