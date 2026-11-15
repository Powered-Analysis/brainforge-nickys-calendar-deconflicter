"""Issue #9: a recurring series is one commitment.

Built on the harness of the conflict-dispatch tests. Google additionally answers
`events.instances` for a series, and answers a window read the way Google does: a series comes
back as one event, carrying `recurrence`, when any of its occurrences overlaps the window.
Occurrences the test lists are the only ones Google knows, so a rule that says otherwise (a
recurrence the occurrences don't follow) shows whether the app computed anything from the rule.
The Sentience MCP session is replaced at the one function that opens it; the database is the
libSQL container.
"""

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import test_conflict_dispatch as dispatch
from dispatcher import google_calendar
from test_calendar_attach import google_error
from test_conflict_dispatch import (  # noqa: F401 (fixtures)
    HOME,
    INSIDE,
    NEXT_TOKEN,
    OUTSIDE,
    SIDE,
    START_TOKEN,
    WORK,
    OverlapGoogle,
    Read,
    auth_requests,
    database,
    meeting,
    notify,
    operator,
    person,
    processed,
    sentience,
    settings_env,
    sha,
    token_of,
    utc,
)

# Fixtures of the dispatch tests, used as they are.
calendars = dispatch.calendars
client = dispatch.client
mcp = dispatch.mcp

NEW_YORK = ZoneInfo("America/New_York")
D1, D2, D3 = "2099-03-02", "2099-03-09", "2099-03-16"
RULE = "RRULE:FREQ=WEEKLY;BYDAY=MO"
LATER_TOKEN = "work-token-2"


def ahead(days):
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


def moment(day, hhmm):
    return datetime.fromisoformat(f"{day}T{hhmm}:00-05:00")


def midnight(day):
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), NEW_YORK)


def at(text):
    return datetime.fromisoformat(text)


class Series(dict):
    """A series event as `events.list` returns it, and the occurrences `events.instances` knows."""

    instances: list


def occurrence(series_id, day, start, end):
    return {
        "kind": "calendar#event",
        "id": f"{series_id}_{day.replace('-', '')}",
        "recurringEventId": series_id,
        "status": "confirmed",
        "start": {"dateTime": f"{day}T{start}:00-05:00"},
        "end": {"dateTime": f"{day}T{end}:00-05:00"},
        "originalStartTime": {"dateTime": f"{day}T{start}:00-05:00"},
    }


def cancelled(series_id, day):
    return {
        "kind": "calendar#event",
        "id": f"{series_id}_{day.replace('-', '')}",
        "recurringEventId": series_id,
        "status": "cancelled",
        "originalStartTime": {"dateTime": f"{day}T10:00:00-05:00"},
    }


def series(
    event_id, days, start="10:00", end="11:00", first="2026-01-05", rule=RULE, **fields
):
    """The series event has the interval of its first occurrence, `first`."""
    event = Series(
        meeting(event_id, start, end, day=first, recurrence=[rule], **fields)
    )
    event.instances = [occurrence(event_id, day, start, end) for day in days]
    return event


def all_day_series(event_id, days, **fields):
    def whole(day, **extra):
        following = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        return {
            "id": f"{event_id}_{day.replace('-', '')}",
            "recurringEventId": event_id,
            "status": "confirmed",
            "start": {"date": day},
            "end": {"date": following},
            **extra,
        }

    event = Series(
        {
            **whole("2026-01-05", **INSIDE, **fields),
            "id": event_id,
            "iCalUID": f"{event_id}@google.com",
            "summary": f"Fictional {event_id}",
            "recurrence": [RULE],
        }
    )
    del event["recurringEventId"]
    event.instances = [whole(day) for day in days]
    return event


def edited(event, **fields):
    copy = Series({**event, **fields})
    copy.instances = event.instances
    return copy


def span(event):
    if "dateTime" in event["start"]:
        return at(event["start"]["dateTime"]), at(event["end"]["dateTime"])
    return midnight(event["start"]["date"]), midnight(event["end"]["date"])


class SeriesGoogle(OverlapGoogle):
    def __init__(self):
        super().__init__()
        self.series = {}
        self.instance_reads = []

    @staticmethod
    def window(query):
        return (
            at(query["timeMin"]),
            at(query.get("timeMax", "9999-12-31T00:00:00+00:00")),
        )

    def route(self, r):
        if r["method"] == "GET":
            instances = re.fullmatch(
                r"/calendar/v3/calendars/(.+)/events/(.+)/instances", r["path"]
            )
            if instances:
                self.instance_reads.append(r)
                calendar_id, series_id = instances.groups()
                held = [
                    s for s in self.series.get(calendar_id, []) if s["id"] == series_id
                ]
                if not held:
                    return google_error(404)
                low, high = self.window(r["query"])
                return 200, {
                    "kind": "calendar#events",
                    "items": [
                        i
                        for i in held[0].instances
                        if "start" not in i or (span(i)[1] > low and span(i)[0] < high)
                    ],
                }
            if r["path"].endswith("/events") and "timeMin" in r["query"]:
                status, payload = super().route(r)
                low, high = self.window(r["query"])
                calendar_id = r["path"].split("/")[4]
                payload["items"] += [
                    s
                    for s in self.series.get(calendar_id, [])
                    if any(
                        "start" in i and span(i)[1] > low and span(i)[0] < high
                        for i in s.instances
                    )
                ]
                return status, payload
        return super().route(r)


@pytest.fixture
def google(monkeypatch):
    fake = SeriesGoogle()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


def place(google, calendar, events):
    """What the calendar holds: series are kept apart, as Google answers for them differently."""
    google.stored[calendar] = [e for e in events if not isinstance(e, Series)]
    google.series[calendar] = [e for e in events if isinstance(e, Series)]


def world(
    google, delta, stored=(), elsewhere=None, token=START_TOKEN, next_token=NEXT_TOKEN
):
    google.script[(WORK, token)] = Read([list(delta)], next_token)
    place(google, WORK, [*delta, *stored])
    for calendar, events in (elsewhere or {}).items():
        place(google, calendar, events)


def again(google, delta, stored=()):
    """The next notification, with its own delta."""
    world(google, delta, stored, token=NEXT_TOKEN, next_token=LATER_TOKEN)


def single(event_id="other", start="10:30", end="11:30", day=D2, **fields):
    return meeting(event_id, start, end, day=day, **fields)


def holds_the_occurrence(entry, day, start, end):
    assert at(entry["start"]) == moment(day, start)
    assert at(entry["end"]) == moment(day, end)


# --- 1. A series is one evaluable event ---------------------------------------------------


def test_a_series_overlapping_a_single_event_is_one_conflict_at_the_colliding_occurrence(
    client, google, calendars, mcp
):
    world(
        google,
        [series("weekly", [D1, D2, D3])],
        [single(day=D2)],
    )
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    payload = mcp.payloads[0]
    assert payload["incoming"]["event_id"] == "weekly"
    assert payload["incoming"]["recurring"] is True
    holds_the_occurrence(payload["incoming"], D2, "10:00", "11:00")
    assert payload["existing"]["event_id"] == "other"
    assert payload["existing"]["recurring"] is False
    assert at(payload["overlap"]["start"]) == moment(D2, "10:30")
    assert at(payload["overlap"]["end"]) == moment(D2, "11:00")
    assert token_of(WORK) == NEXT_TOKEN


def test_a_single_event_overlapping_a_series_it_is_checked_against_is_one_conflict(
    client, google, calendars, mcp
):
    world(google, [single(day=D2)], [series("weekly", [D1, D2, D3])])
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    payload = mcp.payloads[0]
    assert payload["incoming"]["event_id"] == "other"
    assert payload["incoming"]["recurring"] is False
    assert payload["existing"]["event_id"] == "weekly"
    assert payload["existing"]["recurring"] is True
    holds_the_occurrence(payload["existing"], D2, "10:00", "11:00")
    assert at(payload["overlap"]["start"]) == moment(D2, "10:30")
    assert at(payload["overlap"]["end"]) == moment(D2, "11:00")


def test_occurrences_are_read_with_events_instances_and_nothing_is_written(
    client, google, calendars, mcp
):
    world(google, [series("weekly", [D1, D2])], [single(day=D2)])
    notify(client)
    assert google.instance_reads
    assert {r["path"].split("/")[-2] for r in google.instance_reads} == {"weekly"}
    assert google.writes == []
    for read in google.window_reads:
        assert "singleEvents" not in read["query"]


@pytest.mark.parametrize(
    "fields",
    [{"status": "cancelled"}, {"transparency": "transparent"}],
    ids=["cancelled", "transparent"],
)
@pytest.mark.parametrize("role", ["incoming", "checked-against"])
def test_a_cancelled_or_transparent_series_raises_no_conflict(
    client, google, calendars, mcp, fields, role
):
    excluded = series("weekly", [D1, D2], **fields)
    ordinary = single(day=D2)
    if role == "incoming":
        world(google, [excluded], [ordinary])
    else:
        world(google, [ordinary], [excluded])
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert token_of(WORK) == NEXT_TOKEN


def test_a_cancelled_series_the_delta_reports_without_times_raises_no_conflict(
    client, google, calendars, mcp
):
    gone = {"kind": "calendar#event", "id": "weekly", "status": "cancelled"}
    world(google, [gone], [single(day=D2)])
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert token_of(WORK) == NEXT_TOKEN


# --- 2. Occurrences are the ones Google lists, and only the series' own ---------------------


@pytest.mark.parametrize("role", ["incoming", "checked-against"])
@pytest.mark.parametrize(
    ("rule", "fields"),
    [
        (RULE, {"day": "2099-03-03"}),
        (RULE, {"day": D2, "start": "11:00", "end": "12:00"}),
        ("RRULE:FREQ=DAILY", {"day": "2099-03-03"}),
    ],
    ids=["another-day", "touching", "day-the-rule-would-give-but-google-lists-none"],
)
def test_a_single_event_that_meets_no_listed_occurrence_is_no_conflict(
    client, google, calendars, mcp, rule, fields, role
):
    occurrences = series("weekly", [D1, D2], rule=rule)
    ordinary = single(**fields)
    if role == "incoming":
        world(google, [occurrences], [ordinary])
    else:
        world(google, [ordinary], [occurrences])
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert token_of(WORK) == NEXT_TOKEN


@pytest.mark.parametrize("role", ["incoming", "checked-against"])
def test_a_single_event_at_the_time_of_the_series_own_first_occurrence_is_no_conflict(
    client, google, calendars, mcp, role
):
    # The series event carries its first occurrence's interval, which is no occurrence now.
    occurrences = series("weekly", [D2], first=D1)
    ordinary = single(day=D1)
    if role == "incoming":
        world(google, [occurrences], [ordinary])
    else:
        world(google, [ordinary], [occurrences])
    notify(client)
    assert mcp.calls == []


def test_a_cancelled_occurrence_does_not_count(client, google, calendars, mcp):
    weekly = series("weekly", [D1, D2])
    weekly.instances[0] = cancelled("weekly", D1)
    world(google, [weekly], [single(day=D1, start="10:00", end="11:00")])
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert token_of(WORK) == NEXT_TOKEN


def test_a_cancelled_occurrence_does_not_hide_the_others(
    client, google, calendars, mcp
):
    weekly = series("weekly", [D1, D2])
    weekly.instances[0] = cancelled("weekly", D1)
    world(google, [weekly], [single(day=D2)])
    notify(client)
    assert len(mcp.calls) == 1
    holds_the_occurrence(mcp.payloads[0]["incoming"], D2, "10:00", "11:00")


def moved_occurrence():
    return meeting(
        f"weekly_{D1.replace('-', '')}",
        "14:00",
        "15:00",
        day=D1,
        recurringEventId="weekly",
        originalStartTime={"dateTime": f"{D1}T10:00:00-05:00"},
    )


def test_an_occurrence_moved_on_its_own_does_not_count_as_one_of_its_series(
    client, google, calendars, mcp
):
    moved = moved_occurrence()
    weekly = series("weekly", [D2])
    # Google lists the moved occurrence among the instances, at its new time.
    weekly.instances.insert(0, moved)
    world(google, [weekly], [moved, single(day=D1, start="14:30", end="15:30")])
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert token_of(WORK) == NEXT_TOKEN


def test_an_occurrence_moved_on_its_own_counts_once_as_its_own_event(
    client, google, calendars, mcp
):
    moved = moved_occurrence()
    weekly = series("weekly", [D2])
    weekly.instances.insert(0, moved)
    world(google, [weekly, moved], [single(day=D1, start="14:30", end="15:30")])
    notify(client)
    assert len(mcp.calls) == 1
    payload = mcp.payloads[0]
    assert payload["incoming"]["event_id"] == moved["id"]
    assert payload["incoming"]["recurring"] is False
    assert payload["existing"]["event_id"] == "other"


def test_a_series_never_conflicts_with_its_own_moved_occurrence(
    client, google, calendars, mcp
):
    moved = meeting(
        "weekly_moved",
        "10:30",
        "11:30",
        day=D2,
        recurringEventId="weekly",
        originalStartTime={"dateTime": f"{D2}T10:00:00-05:00"},
    )
    world(google, [series("weekly", [D1, D2])], [moved])
    notify(client)
    assert mcp.calls == []
    again(google, [moved], [series("weekly", [D1, D2])])
    notify(client)
    assert mcp.calls == []


def test_a_series_never_conflicts_with_a_copy_of_its_invitation(
    client, google, calendars, mcp
):
    shared = "shared-series@google.com"
    copy = series("copy", [D1, D2], iCalUID=shared)
    world(
        google,
        [series("weekly", [D1, D2], iCalUID=shared)],
        elsewhere={HOME: [copy]},
    )
    assert notify(client).status_code == 200
    assert mcp.calls == []


def test_a_series_is_one_event_for_each_pair_it_is_in(client, google, calendars, mcp):
    world(
        google,
        [series("weekly", [D1, D2, D3])],
        [
            single("first", day=D1),
            single("second", day=D2),
            single("third", day=D3),
            single("clear", day=D3, start="12:00", end="13:00"),
        ],
    )
    notify(client)
    assert len(mcp.calls) == 3
    by_other = {p["existing"]["event_id"]: p for p in mcp.payloads}
    assert set(by_other) == {"first", "second", "third"}
    for name, day in (("first", D1), ("second", D2), ("third", D3)):
        holds_the_occurrence(by_other[name]["incoming"], day, "10:00", "11:00")
    assert len(processed()) == 3


# --- 3. Series against series: the 90-day look-ahead --------------------------------------


def weeks(first, count=3):
    return [ahead(first + 7 * n) for n in range(count)]


def test_two_series_that_collide_every_week_raise_one_conflict(
    client, google, calendars, mcp
):
    mine = series("mine", weeks(10), first=ahead(-28))
    theirs = series("theirs", weeks(10), start="10:30", end="11:30", first=ahead(-28))
    world(google, [mine], [theirs])
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    payload = mcp.payloads[0]
    assert payload["incoming"]["event_id"] == "mine"
    assert payload["incoming"]["recurring"] is True
    assert payload["existing"]["event_id"] == "theirs"
    assert payload["existing"]["recurring"] is True
    first = ahead(10)
    holds_the_occurrence(payload["incoming"], first, "10:00", "11:00")
    holds_the_occurrence(payload["existing"], first, "10:30", "11:30")
    assert at(payload["overlap"]["start"]) == moment(first, "10:30")
    assert at(payload["overlap"]["end"]) == moment(first, "11:00")
    assert len(processed()) == 1


def test_the_other_series_being_the_incoming_one_gives_the_same_single_conflict(
    client, google, calendars, mcp
):
    mine = series("mine", weeks(10), first=ahead(-28))
    theirs = series("theirs", weeks(10), start="10:30", end="11:30", first=ahead(-28))
    world(google, [mine], [theirs])
    notify(client)
    first_hash = next(iter(processed()))
    again(google, [theirs], [mine])
    notify(client)
    assert len(mcp.calls) == 1
    assert list(processed()) == [first_hash]


def test_series_that_first_collide_beyond_90_days_raise_nothing(
    client, google, calendars, mcp
):
    mine = series("mine", weeks(100), first=ahead(-28))
    theirs = series("theirs", weeks(100), start="10:30", end="11:30", first=ahead(-28))
    world(google, [mine], [theirs])
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert processed() == {}
    assert token_of(WORK) == NEXT_TOKEN


def test_series_that_collide_inside_90_days_are_raised_at_the_earliest_occurrence(
    client, google, calendars, mcp
):
    mine = series("mine", [ahead(80), ahead(100)], first=ahead(-28))
    theirs = series(
        "theirs", [ahead(80), ahead(100)], start="10:30", end="11:30", first=ahead(-28)
    )
    world(google, [mine], [theirs])
    notify(client)
    assert len(mcp.calls) == 1
    holds_the_occurrence(mcp.payloads[0]["incoming"], ahead(80), "10:00", "11:00")


def test_a_collision_already_past_is_not_raised_and_the_next_one_is(
    client, google, calendars, mcp
):
    mine = series("mine", [ahead(-7), ahead(7)], first=ahead(-28))
    theirs = series(
        "theirs", [ahead(-7), ahead(7)], start="10:30", end="11:30", first=ahead(-28)
    )
    world(google, [mine], [theirs])
    notify(client)
    assert len(mcp.calls) == 1
    holds_the_occurrence(mcp.payloads[0]["incoming"], ahead(7), "10:00", "11:00")
    holds_the_occurrence(mcp.payloads[0]["existing"], ahead(7), "10:30", "11:30")


def test_series_that_collided_only_in_the_past_raise_nothing(
    client, google, calendars, mcp
):
    mine = series("mine", [ahead(-7)], first=ahead(-28))
    theirs = series("theirs", [ahead(-7)], start="10:30", end="11:30", first=ahead(-28))
    world(google, [mine], [theirs])
    assert notify(client).status_code == 200
    assert mcp.calls == []


def test_series_that_never_share_a_time_raise_nothing(client, google, calendars, mcp):
    mine = series("mine", weeks(10), first=ahead(-28))
    theirs = series("theirs", weeks(11), first=ahead(-28))
    world(google, [mine], [theirs])
    notify(client)
    assert mcp.calls == []


# --- 4. Classification and cross-calendar --------------------------------------------------


@pytest.mark.parametrize(
    ("series_fields", "single_fields", "policy", "classes", "role"),
    [
        (
            OUTSIDE,
            INSIDE,
            "never_move_external",
            ("external_mixed", "internal"),
            "incoming",
        ),
        (
            INSIDE,
            OUTSIDE,
            "never_move_external",
            ("internal", "external_mixed"),
            "incoming",
        ),
        (OUTSIDE, OUTSIDE, "irreconcilable", ("external_mixed",) * 2, "incoming"),
        (INSIDE, INSIDE, "full_evaluation", ("internal", "internal"), "incoming"),
        (
            OUTSIDE,
            INSIDE,
            "never_move_external",
            ("external_mixed", "internal"),
            "checked-against",
        ),
    ],
    ids=[
        "series-outside",
        "single-outside",
        "both-outside",
        "both-inside",
        "series-outside-checked-against",
    ],
)
def test_a_series_is_classified_by_its_own_attendees(
    client, google, calendars, mcp, series_fields, single_fields, policy, classes, role
):
    # The occurrences carry no attendees: the series event is what classifies.
    weekly = series("weekly", [D2], **series_fields)
    ordinary = single(**single_fields)
    if role == "incoming":
        world(google, [weekly], [ordinary])
        series_side, single_side = "incoming", "existing"
    else:
        world(google, [ordinary], [weekly])
        series_side, single_side = "existing", "incoming"
    notify(client)
    payload = mcp.payloads[0]
    assert payload["policy"] == policy
    assert (
        payload[series_side]["classification"],
        payload[single_side]["classification"],
    ) == classes
    assert payload[series_side]["organizer"] == series_fields["organizer"]["email"]
    assert payload[series_side]["attendees"] == [
        a["email"] for a in series_fields["attendees"]
    ]


@pytest.mark.parametrize("role", ["incoming", "checked-against"])
def test_a_series_colliding_across_calendars_is_internal_on_both_sides(
    client, google, calendars, mcp, role
):
    weekly = series("weekly", [D2], **OUTSIDE)
    ordinary = single(**OUTSIDE)
    if role == "incoming":
        world(google, [weekly], elsewhere={HOME: [ordinary]})
    else:
        world(google, [ordinary], elsewhere={HOME: [weekly]})
    notify(client)
    assert len(mcp.calls) == 1
    payload = mcp.payloads[0]
    assert payload["cross_calendar"] is True
    assert payload["policy"] == "full_evaluation"
    assert payload["incoming"]["classification"] == "internal"
    assert payload["existing"]["classification"] == "internal"
    series_side = "incoming" if role == "incoming" else "existing"
    assert payload[series_side]["recurring"] is True
    holds_the_occurrence(payload[series_side], D2, "10:00", "11:00")


def test_a_series_on_a_calendar_that_is_not_active_is_not_checked(
    client, google, calendars, mcp
):
    world(google, [single(day=D2)], elsewhere={SIDE: [series("weekly", [D2])]})
    notify(client)
    assert mcp.calls == []


# --- 5. All-day series ---------------------------------------------------------------------


def test_an_all_day_series_occupies_whole_days_of_the_primary_zone(
    client, google, calendars, mcp
):
    weekly = all_day_series("holiday", [D1, D2])
    world(google, [weekly], [single(day=D2, start="15:00", end="16:00")])
    notify(client)
    assert len(mcp.calls) == 1
    payload = mcp.payloads[0]
    assert payload["incoming"]["all_day"] is True
    assert payload["incoming"]["recurring"] is True
    assert at(payload["incoming"]["start"]) == midnight(D2)
    assert at(payload["incoming"]["end"]) == midnight(
        (date.fromisoformat(D2) + timedelta(days=1)).isoformat()
    )


def test_the_exclusive_end_date_of_an_all_day_occurrence_is_not_busy(
    client, google, calendars, mcp
):
    weekly = all_day_series("holiday", [D1])
    following_day = (date.fromisoformat(D1) + timedelta(days=1)).isoformat()
    world(google, [weekly], [single(day=following_day, start="00:00", end="01:00")])
    notify(client)
    assert mcp.calls == []


def test_a_transparent_all_day_series_raises_no_conflict(
    client, google, calendars, mcp
):
    weekly = all_day_series("holiday", [D1], transparency="transparent")
    world(google, [weekly], [single(day=D1, start="09:00", end="10:00")])
    notify(client)
    assert mcp.calls == []


# --- 6. De-duplication ---------------------------------------------------------------------


PRD_SERIES = "7hd02mzexample"
PRD_SINGLE = "4kq1t8f0example"
PRD_FIRST = "2026-01-05"


def prd_example(*recurrence):
    """The PRD's second example, with the colliding occurrence on a day still ahead."""
    weekly = series(
        PRD_SERIES, [D1], start="09:30", end="10:30", first=PRD_FIRST, rule=RULE
    )
    if recurrence:
        weekly = edited(weekly, recurrence=list(recurrence))
    vendor = single(PRD_SINGLE, "10:00", "11:00", day=D1, **OUTSIDE)
    return weekly, vendor


def prd_string(recurrence):
    return (
        f"{WORK}|{PRD_SINGLE}|{utc(D1, '15:00')}|{utc(D1, '16:00')}||external_mixed|"
        f"{WORK}|{PRD_SERIES}|{utc(PRD_FIRST, '14:30')}|{utc(PRD_FIRST, '15:30')}|"
        f"{recurrence}|internal"
    )


def test_the_prd_example_string_hashes_to_the_prd_hash():
    string = (
        "operator@example.com|4kq1t8f0example|2026-03-02T15:00:00Z|2026-03-02T16:00:00Z"
        "||external_mixed|operator@example.com|7hd02mzexample|2026-01-05T14:30:00Z|"
        "2026-01-05T15:30:00Z|RRULE:FREQ=WEEKLY;BYDAY=MO|internal"
    )
    assert sha(string) == (
        "32118fb0b34f3641bfbd1dde3cddd11f183d0318b3749c662d02f9d342228852"
    )


@pytest.mark.parametrize("role", ["series-incoming", "single-incoming"])
def test_a_series_conflict_is_recorded_under_the_hash_of_the_series_own_schedule(
    client, google, calendars, mcp, role
):
    weekly, vendor = prd_example()
    if role == "series-incoming":
        world(google, [weekly], [vendor])
    else:
        world(google, [vendor], [weekly])
    notify(client)
    assert len(mcp.calls) == 1
    assert list(processed()) == [sha(prd_string(RULE))]


def test_a_series_with_several_rule_lines_hashes_them_in_googles_order_with_one_space(
    client, google, calendars, mcp
):
    lines = (RULE, "EXDATE;TZID=America/New_York:20990309T093000")
    weekly, vendor = prd_example(*lines)
    world(google, [weekly], [vendor])
    notify(client)
    assert list(processed()) == [sha(prd_string(" ".join(lines)))]


def test_a_replayed_series_conflict_is_dispatched_once(client, google, calendars, mcp):
    weekly = series("weekly", [D1, D2, D3])
    ordinary = single(day=D2)
    world(google, [weekly], [ordinary])
    notify(client)
    again(google, [weekly], [ordinary])
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    assert token_of(WORK) == LATER_TOKEN


def test_a_series_conflict_is_not_dispatched_again_as_its_earliest_occurrence_passes(
    client, google, calendars, mcp
):
    mine = series("mine", weeks(10), first=ahead(-28))
    theirs = series("theirs", weeks(10), start="10:30", end="11:30", first=ahead(-28))
    world(google, [mine], [theirs])
    notify(client)
    assert len(mcp.calls) == 1
    # A week on, the occurrence that collided first is over, and the pair still collides.
    mine.instances = mine.instances[1:]
    theirs.instances = theirs.instances[1:]
    again(google, [mine], [theirs])
    notify(client)
    assert len(mcp.calls) == 1


SCHEDULE_CHANGES = {
    "recurrence": {"recurrence": ["RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO"]},
    "start": {"start": {"dateTime": "2026-01-05T09:00:00-05:00"}},
    "end": {"end": {"dateTime": "2026-01-05T11:30:00-05:00"}},
    "classification": OUTSIDE,
}


@pytest.mark.parametrize(
    "change", SCHEDULE_CHANGES.values(), ids=SCHEDULE_CHANGES.keys()
)
def test_a_series_conflict_is_dispatched_again_when_its_schedule_or_classification_changes(
    client, google, calendars, mcp, change
):
    weekly = series("weekly", [D2])
    ordinary = single(day=D2)
    world(google, [weekly], [ordinary])
    notify(client)
    changed = edited(weekly, **change)
    again(google, [changed], [ordinary])
    notify(client)
    assert len(mcp.calls) == 2
    assert len(processed()) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"summary": "Renamed series"},
        {"attendees": [person("operator@example.com"), person("kim@example.com")]},
    ],
    ids=["title", "attendees-still-internal"],
)
def test_a_series_conflict_is_not_dispatched_again_for_a_change_that_leaves_the_hash(
    client, google, calendars, mcp, change
):
    weekly = series("weekly", [D2])
    ordinary = single(day=D2)
    world(google, [weekly], [ordinary])
    notify(client)
    again(google, [edited(weekly, **change)], [ordinary])
    notify(client)
    assert len(mcp.calls) == 1


def test_editing_a_single_occurrence_does_not_dispatch_the_series_conflict_again(
    client, google, calendars, mcp
):
    weekly = series("weekly", [D1, D2, D3])
    ordinary = single(day=D2)
    world(google, [weekly], [ordinary])
    notify(client)
    assert len(mcp.calls) == 1
    # The occurrence of D3 is moved to a free time; Google reports only that occurrence.
    moved = meeting(
        f"weekly_{D3.replace('-', '')}",
        "14:00",
        "15:00",
        day=D3,
        recurringEventId="weekly",
        originalStartTime={"dateTime": f"{D3}T10:00:00-05:00"},
    )
    again(google, [moved], [weekly, ordinary])
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    assert token_of(WORK) == LATER_TOKEN
