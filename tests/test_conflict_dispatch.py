"""Issue #6: a notification's delta is checked for conflicts, and each one is delivered.

Google is the `httplib2`-compatible fake the webhook tests use, extended to answer the
overlap reads (`events.list` with `timeMin` and `timeMax`) the way Google does. The Sentience
MCP session is replaced at the one function that opens it and dispatches a payload. The
database is the libSQL container.
"""

import hashlib
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from dispatcher import google_calendar, sentience_mcp
from dispatcher.app import app
from test_calendar_attach import (  # noqa: F401 (fixtures)
    WORK,
    database,
    execute,
    google_error,
    rows,
    sentience,
    settings_env,
)
from test_calendar_attach import seed as seed_calendar
from test_gcal_webhook import (
    HOME,
    HOME_CHANNEL,
    SIDE,
    SIDE_CHANNEL,
    WORK_CHANNEL,
    DeltaGoogle,
    Read,
    notify,
    token_of,
)

ACCESS_TOKEN = "sentience-access-token-example"
FUTURE = "2099-03-02"
PAST = "2020-03-02"
START_TOKEN = "work-token-0"
NEXT_TOKEN = "work-token-1"


class OverlapGoogle(DeltaGoogle):
    """Adds the events a calendar holds, and answers a `timeMin`/`timeMax` read with those that
    overlap the window, as Google does: an event ending at `timeMin` or starting at `timeMax`
    is left out."""

    def __init__(self):
        super().__init__()
        self.stored = {}
        self.window_reads = []
        self.add(
            {
                "id": WORK,
                "summary": "Fictional Work",
                "accessRole": "owner",
                "timeZone": "America/New_York",
            },
            alias="primary",
        )

    def route(self, r):
        if (
            r["method"] == "GET"
            and r["path"].endswith("/events")
            and "timeMin" in r["query"]
        ):
            calendar_id = r["path"].split("/")[4]
            self.window_reads.append(r)
            low = datetime.fromisoformat(r["query"]["timeMin"])
            high = datetime.fromisoformat(
                r["query"].get("timeMax", "9999-12-31T00:00:00+00:00")
            )
            items = []
            for stored in self.stored.get(calendar_id, []):
                if "dateTime" in stored.get("start", {}):
                    begins = datetime.fromisoformat(stored["start"]["dateTime"])
                    ends = datetime.fromisoformat(stored["end"]["dateTime"])
                    if not (ends > low and begins < high):
                        continue
                else:
                    # An all-day event is whole days of the primary zone (`end.date` excluded).
                    zone = ZoneInfo("America/New_York")
                    begins = datetime.fromisoformat(stored["start"]["date"]).replace(
                        tzinfo=zone
                    )
                    ends = datetime.fromisoformat(stored["end"]["date"]).replace(
                        tzinfo=zone
                    )
                    if not (ends > low and begins < high):
                        continue
                items.append(stored)
            return 200, {"kind": "calendar#events", "items": items}
        return super().route(r)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def calendars():
    seed_calendar(WORK, 1, WORK_CHANNEL, START_TOKEN)
    seed_calendar(HOME, 1, HOME_CHANNEL, "home-token-0")
    seed_calendar(SIDE, 0, SIDE_CHANNEL, "side-token-0")


@pytest.fixture
def auth_requests(sentience):  # noqa: F811
    return sentience


@pytest.fixture
def google(monkeypatch):
    fake = OverlapGoogle()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


class Sentience:
    """Replaces the Sentience MCP session: records each dispatch and can be told to fail."""

    def __init__(self):
        self.calls = []
        self.fail_on = set()

    def __call__(self, access_token, message):
        index = len(self.calls)
        self.calls.append((access_token, message))
        if index in self.fail_on:
            raise sentience_mcp.DispatchFailed("fictional failure")

    @property
    def payloads(self):
        return [payload_of(message) for _, message in self.calls]


@pytest.fixture
def mcp(monkeypatch):
    fake = Sentience()
    monkeypatch.setattr(sentience_mcp, "dispatch", fake)
    monkeypatch.setenv("SENTIENCE_THREAD_ID", "thread-example-0001")
    return fake


@pytest.fixture(autouse=True)
def operator(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_DOMAIN", "example.com")
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    execute("DELETE FROM sentience_auth")
    execute(
        "INSERT INTO sentience_auth (id, client_id, access_token, refresh_token, expires_at, updated_at)"
        " VALUES ('sentience', 'client-example', ?, 'refresh-example', 4102444800, 1700000000)",
        (ACCESS_TOKEN,),
    )
    yield
    execute("DELETE FROM sentience_auth")


def payload_of(message):
    block = message.split("\n\n", 2)[2]
    assert block.startswith("```json\n")
    assert block.endswith("\n```")
    return json.loads(block.removeprefix("```json\n").removesuffix("\n```"))


def person(email, **flags):
    return {"email": email, **flags}


INSIDE = {
    "organizer": person("operator@example.com"),
    "attendees": [person("operator@example.com"), person("sam@example.com")],
}
OUTSIDE = {
    "organizer": person("pat@example.org"),
    "attendees": [person("operator@example.com"), person("pat@example.org")],
}


def meeting(event_id, start, end, day=FUTURE, offset="-05:00", **fields):
    return {
        "kind": "calendar#event",
        "id": event_id,
        "iCalUID": f"{event_id}@google.com",
        "status": "confirmed",
        "summary": f"Fictional {event_id}",
        "start": {"dateTime": f"{day}T{start}:00{offset}"},
        "end": {"dateTime": f"{day}T{end}:00{offset}"},
        **INSIDE,
        **fields,
    }


def world(
    google, delta, stored=(), calendar=WORK, token=START_TOKEN, next_token=NEXT_TOKEN
):
    """The delta Google returns for the calendar, and what the calendar holds."""
    google.script[(calendar, token)] = Read([list(delta)], next_token)
    google.stored[calendar] = [*delta, *stored]


def processed():
    return {
        r[0]: (r[1], r[2])
        for r in execute(
            "SELECT event_hash, calendar_id, processed_at FROM processed_events"
        )
    }


def sha(*fields):
    return hashlib.sha256("|".join(fields).encode()).hexdigest()


def utc(day, hhmm):
    return f"{day}T{hhmm}:00Z"


def prd_hash(first, second):
    """The PRD's hash of two plain meetings given as (calendar, id, start, end, class) tuples."""
    ordered = sorted((first, second), key=lambda m: (m[0], m[1]))
    return sha(*[f for m in ordered for f in (m[0], m[1], m[2], m[3], "", m[4])])


# --- Evaluation and the overlap read (criterion 1) -----------------------------------------


def test_a_changed_event_overlapping_another_is_dispatched_and_the_cursor_moves(
    client, google, calendars, mcp
):
    changed = meeting("changed", "10:00", "11:00")
    world(google, [changed], [meeting("other", "10:30", "11:30")])
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    assert token_of(WORK) == NEXT_TOKEN


def test_the_overlap_is_read_from_every_active_calendar_with_a_bounded_window_and_no_expansion(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    notify(client)
    assert google.window_reads
    assert {read["path"].split("/")[4] for read in google.window_reads} == {WORK, HOME}
    for read in google.window_reads:
        assert "singleEvents" not in read["query"]
        assert datetime.fromisoformat(
            read["query"]["timeMin"]
        ) <= datetime.fromisoformat(f"{FUTURE}T10:00:00-05:00")
        assert datetime.fromisoformat(
            read["query"]["timeMax"]
        ) >= datetime.fromisoformat(f"{FUTURE}T11:00:00-05:00")


def test_the_conflict_is_read_when_the_delta_is_processed_not_taken_from_the_delta(
    client, google, calendars, mcp
):
    # The other event is in no delta; only the overlap read can find it.
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("quiet", "10:15", "10:45")],
    )
    notify(client)
    payload = mcp.payloads[0]
    assert payload["incoming"]["event_id"] == "changed"
    assert payload["existing"]["event_id"] == "quiet"


def test_a_notification_makes_no_write_to_google(client, google, calendars, mcp):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    notify(client)
    assert google.writes == []


def test_the_full_read_after_a_410_is_evaluated_too(client, google, calendars, mcp):
    other = meeting("other", "10:30", "11:30")
    changed = meeting("changed", "10:00", "11:00")
    google.script[(WORK, START_TOKEN)] = 410
    google.script[(WORK, None)] = Read([[changed, other]], NEXT_TOKEN)
    google.stored[WORK] = [changed, other]
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    assert token_of(WORK) == NEXT_TOKEN


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "cancelled"},
        {"transparency": "transparent"},
        {
            "start": {"date": f"{FUTURE}"},
            "end": {"date": "2099-03-03"},
            "transparency": "transparent",
        },
    ],
    ids=["cancelled", "transparent", "all-day-transparent"],
)
def test_an_excluded_event_raises_no_conflict_whether_it_changed_or_was_overlapped(
    client, google, calendars, mcp, fields
):
    ordinary = meeting("ordinary", "10:00", "11:00")
    excluded = {**meeting("excluded", "10:30", "11:30"), **fields}
    world(google, [excluded], [ordinary])
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert token_of(WORK) == NEXT_TOKEN
    google.script[(WORK, NEXT_TOKEN)] = Read([[ordinary]], "work-token-2")
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert token_of(WORK) == "work-token-2"


def test_an_event_with_transparency_opaque_or_unset_is_evaluated(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00", transparency="opaque")],
        [meeting("other", "10:30", "11:30")],
    )
    notify(client)
    assert len(mcp.calls) == 1


def test_a_moved_occurrence_of_a_series_is_an_ordinary_event(
    client, google, calendars, mcp
):
    occurrence = meeting(
        "series_20990302",
        "10:00",
        "11:00",
        recurringEventId="series",
        originalStartTime={"dateTime": f"{FUTURE}T09:00:00-05:00"},
    )
    world(google, [occurrence], [meeting("other", "10:30", "11:30")])
    notify(client)
    assert len(mcp.calls) == 1
    assert mcp.payloads[0]["incoming"]["event_id"] == "series_20990302"
    assert mcp.payloads[0]["incoming"]["recurring"] is False


# --- Overlap rules (criteria 2, 3, 4, 7) ---------------------------------------------------


def test_meetings_that_only_touch_do_not_conflict(client, google, calendars, mcp):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("after", "11:00", "12:00"), meeting("before", "09:00", "10:00")],
    )
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert token_of(WORK) == NEXT_TOKEN


def test_an_event_overlapping_three_others_gives_three_dispatches_with_it_incoming(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "12:00")],
        [
            meeting("one", "09:00", "10:30"),
            meeting("two", "10:30", "11:00"),
            meeting("three", "11:30", "13:00"),
            meeting("clear", "12:00", "13:00"),
        ],
    )
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 3
    assert {p["incoming"]["event_id"] for p in mcp.payloads} == {"changed"}
    assert {p["existing"]["event_id"] for p in mcp.payloads} == {"one", "two", "three"}
    assert len(processed()) == 3


def test_an_overlap_that_has_already_ended_is_not_raised_and_the_cursor_still_moves(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00", day=PAST)],
        [meeting("other", "10:30", "11:30", day=PAST)],
    )
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert processed() == {}
    assert token_of(WORK) == NEXT_TOKEN


def test_the_event_the_delta_returned_is_never_its_own_conflict(
    client, google, calendars, mcp
):
    # Google's overlap read returns the changed event itself among the overlapping events.
    world(google, [meeting("changed", "10:00", "11:00")])
    assert notify(client).status_code == 200
    assert mcp.calls == []


def test_another_copy_of_the_same_invitation_is_not_a_conflict(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00", iCalUID="shared@google.com")],
        [meeting("copy", "10:30", "11:30", iCalUID="shared@google.com")],
    )
    assert notify(client).status_code == 200
    assert mcp.calls == []


# --- Classification and policy reach the message (criteria 5, 6) ---------------------------


@pytest.mark.parametrize(
    ("changed", "other", "policy", "classes"),
    [
        (OUTSIDE, OUTSIDE, "irreconcilable", ("external_mixed", "external_mixed")),
        (OUTSIDE, INSIDE, "never_move_external", ("external_mixed", "internal")),
        (INSIDE, OUTSIDE, "never_move_external", ("internal", "external_mixed")),
        (INSIDE, INSIDE, "full_evaluation", ("internal", "internal")),
    ],
    ids=["both-outside", "incoming-outside", "existing-outside", "both-inside"],
)
def test_policy_and_classifications_follow_the_participants(
    client, google, calendars, mcp, changed, other, policy, classes
):
    world(
        google,
        [meeting("changed", "10:00", "11:00", **changed)],
        [meeting("other", "10:30", "11:30", **other)],
    )
    notify(client)
    payload = mcp.payloads[0]
    assert payload["policy"] == policy
    assert (
        payload["incoming"]["classification"],
        payload["existing"]["classification"],
    ) == classes


def test_an_outside_organizer_with_hidden_guests_makes_the_meeting_external(
    client, google, calendars, mcp
):
    hidden = {
        "organizer": person("pat@example.org"),
        "attendees": [person("operator@example.com")],
    }
    world(
        google,
        [meeting("changed", "10:00", "11:00", **hidden)],
        [meeting("other", "10:30", "11:30")],
    )
    notify(client)
    assert mcp.payloads[0]["incoming"]["classification"] == "external_mixed"


def test_the_workspace_domain_setting_decides_who_is_inside(
    client, google, calendars, mcp, monkeypatch
):
    monkeypatch.setenv("GOOGLE_WORKSPACE_DOMAIN", "example.org")
    org_only = {
        "organizer": person("pat@example.org"),
        "attendees": [person("pat@example.org"), person("lee@example.org")],
    }
    world(
        google,
        [meeting("changed", "10:00", "11:00", **org_only)],
        [meeting("other", "10:30", "11:30", **INSIDE)],
    )
    notify(client)
    payload = mcp.payloads[0]
    assert payload["incoming"]["classification"] == "internal"
    assert payload["existing"]["classification"] == "external_mixed"


# --- The message and its payload (criteria 8, 9) -------------------------------------------


def test_the_message_has_the_three_parts_and_the_primary_zones_offsets(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00", offset="-04:00", day="2099-07-01")],
        [meeting("other", "10:30", "11:30", offset="-04:00", day="2099-07-01")],
    )
    notify(client)
    _, message = mcp.calls[0]
    instruction, sentence, block = message.split("\n\n", 2)
    assert instruction.startswith(
        "Automated message from the calendar conflict dispatcher."
    )
    assert sentence == (
        "Policy: both meetings are Internal. Weigh their inertia and suggest new times for "
        "the more flexible one."
    )
    assert block.startswith("```json\n") and block.endswith("\n```")
    payload = payload_of(message)
    assert payload["cross_calendar"] is False
    assert payload["overlap"] == {
        "start": "2099-07-01T10:30:00-04:00",
        "end": "2099-07-01T11:00:00-04:00",
    }
    assert payload["incoming"]["start"] == "2099-07-01T10:00:00-04:00"


def test_the_offset_is_the_primary_calendars_zone_not_the_events(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "15:00", "16:00", offset="+00:00")],
        [meeting("other", "15:30", "16:30", offset="+00:00")],
    )
    notify(client)
    payload = mcp.payloads[0]
    assert payload["incoming"]["start"] == f"{FUTURE}T10:00:00-05:00"
    assert payload["overlap"]["end"] == f"{FUTURE}T11:00:00-05:00"


def test_the_primary_zone_is_the_one_google_reports_for_the_primary_calendar(
    client, google, calendars, mcp
):
    google.entries[WORK]["timeZone"] = "Asia/Tokyo"
    world(
        google,
        [meeting("changed", "10:00", "11:00", offset="+09:00")],
        [meeting("other", "10:30", "11:30", offset="+09:00")],
    )
    notify(client)
    assert mcp.payloads[0]["incoming"]["start"] == f"{FUTURE}T10:00:00+09:00"


def test_only_the_titles_and_identifiers_leave_the_app(client, google, calendars, mcp):
    world(
        google,
        [
            meeting(
                "changed",
                "10:00",
                "11:00",
                description="DESCRIPTION-MUST-NOT-LEAK",
                location="LOCATION-MUST-NOT-LEAK",
                hangoutLink="https://meet.example.com/LINK-MUST-NOT-LEAK",
            )
        ],
        [meeting("other", "10:30", "11:30", description="OTHER-MUST-NOT-LEAK")],
    )
    notify(client)
    assert "MUST-NOT-LEAK" not in mcp.calls[0][1]
    assert "meet.example.com" not in mcp.calls[0][1]


# --- Dispatch (criteria 11, 12, 16) --------------------------------------------------------


def test_the_stored_access_token_and_the_message_are_all_the_dispatch_gets(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    notify(client)
    ((token, message),) = mcp.calls
    assert token == ACCESS_TOKEN
    assert message.startswith(
        "Automated message from the calendar conflict dispatcher."
    )


def test_a_delivered_conflict_is_recorded_with_its_hash_calendar_and_delivery_time(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    before = int(time.time())
    notify(client)
    after = int(time.time())
    expected = prd_hash(
        (WORK, "changed", utc(FUTURE, "15:00"), utc(FUTURE, "16:00"), "internal"),
        (WORK, "other", utc(FUTURE, "15:30"), utc(FUTURE, "16:30"), "internal"),
    )
    recorded = processed()
    assert list(recorded) == [expected]
    calendar_id, processed_at = recorded[expected]
    assert calendar_id == WORK
    assert before <= processed_at <= after


def test_the_recorded_calendar_is_the_incoming_events_calendar(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("other", "10:30", "11:30")],
        [meeting("changed", "10:00", "11:00")],
    )
    notify(client)
    assert {calendar for calendar, _ in processed().values()} == {WORK}


def test_the_dispatcher_never_writes_to_google_or_talks_to_sentiences_auth_server(
    client, google, calendars, mcp, auth_requests
):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    notify(client)
    assert google.writes == []
    assert auth_requests == []


# --- De-duplication (criteria 13, 15) ------------------------------------------------------


def test_a_conflict_already_recorded_is_not_dispatched_again(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    google.script[(WORK, NEXT_TOKEN)] = Read(
        [[meeting("changed", "10:00", "11:00")]], "work-token-2"
    )
    notify(client)
    assert len(mcp.calls) == 1
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    assert token_of(WORK) == "work-token-2"


def test_a_conflict_found_twice_in_one_delta_is_dispatched_once(
    client, google, calendars, mcp
):
    first = meeting("first", "10:00", "11:00")
    second = meeting("second", "10:30", "11:30")
    world(google, [first, second])
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    assert len(processed()) == 1


def test_a_conflict_recorded_earlier_is_not_dispatched_after_a_410_full_read(
    client, google, calendars, mcp
):
    changed = meeting("changed", "10:00", "11:00")
    other = meeting("other", "10:30", "11:30")
    world(google, [changed], [other])
    notify(client)
    google.script[(WORK, NEXT_TOKEN)] = 410
    google.script[(WORK, None)] = Read([[changed, other]], "work-token-2")
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1


def test_a_title_change_dispatches_nothing_new(client, google, calendars, mcp):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    notify(client)
    retitled = meeting("changed", "10:00", "11:00", summary="A new title")
    google.stored[WORK] = [retitled, meeting("other", "10:30", "11:30")]
    google.script[(WORK, NEXT_TOKEN)] = Read([[retitled]], "work-token-2")
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1


def test_an_attendee_change_that_keeps_the_classification_dispatches_nothing_new(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    notify(client)
    other_guests = meeting(
        "changed",
        "10:00",
        "11:00",
        attendees=[person("operator@example.com"), person("kim@example.com")],
    )
    google.stored[WORK] = [other_guests, meeting("other", "10:30", "11:30")]
    google.script[(WORK, NEXT_TOKEN)] = Read([[other_guests]], "work-token-2")
    notify(client)
    assert len(mcp.calls) == 1


@pytest.mark.parametrize(
    ("moved", "start", "end"),
    [
        ("changed", "10:15", "11:00"),
        ("changed", "10:00", "11:15"),
        ("other", "10:45", "11:30"),
    ],
    ids=["incoming-start", "incoming-end", "existing-start"],
)
def test_a_time_change_to_either_meeting_dispatches_the_conflict_again(
    client, google, calendars, mcp, moved, start, end
):
    changed = meeting("changed", "10:00", "11:00")
    other = meeting("other", "10:30", "11:30")
    world(google, [changed], [other])
    notify(client)
    new = meeting(moved, start, end)
    stored = {"changed": changed, "other": other, moved: new}
    google.stored[WORK] = list(stored.values())
    google.script[(WORK, NEXT_TOKEN)] = Read([[new]], "work-token-2")
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 2
    assert len(processed()) == 2


def test_a_classification_change_dispatches_the_conflict_again(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    notify(client)
    outside = meeting("changed", "10:00", "11:00", **OUTSIDE)
    google.stored[WORK] = [outside, meeting("other", "10:30", "11:30")]
    google.script[(WORK, NEXT_TOKEN)] = Read([[outside]], "work-token-2")
    notify(client)
    assert len(mcp.calls) == 2
    assert mcp.payloads[1]["policy"] == "never_move_external"


# --- Failure (criterion 14) ----------------------------------------------------------------


def test_a_failed_dispatch_leaves_no_row_and_the_cursor_unmoved_and_answers_503(
    client, google, calendars, mcp
):
    mcp.fail_on = {0}
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    assert notify(client).status_code == 503
    assert processed() == {}
    assert token_of(WORK) == START_TOKEN


def test_the_retry_after_a_failure_dispatches_the_conflict(
    client, google, calendars, mcp
):
    mcp.fail_on = {0}
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    assert notify(client).status_code == 503
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 2
    assert len(processed()) == 1
    assert token_of(WORK) == NEXT_TOKEN


def test_no_stored_sentience_token_answers_503_without_dispatching(
    client, google, calendars, mcp
):
    execute("DELETE FROM sentience_auth")
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    assert notify(client).status_code == 503
    assert mcp.calls == []
    assert processed() == {}
    assert token_of(WORK) == START_TOKEN


def test_no_sentience_token_does_not_matter_when_there_is_no_conflict(
    client, google, calendars, mcp
):
    execute("DELETE FROM sentience_auth")
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("later", "12:00", "13:00")],
    )
    assert notify(client).status_code == 200
    assert token_of(WORK) == NEXT_TOKEN


def test_a_failure_partway_keeps_the_earlier_rows_and_the_retry_sends_only_the_rest(
    client, google, calendars, mcp
):
    mcp.fail_on = {1}
    world(
        google,
        [meeting("changed", "10:00", "12:00")],
        [meeting("one", "09:00", "10:30"), meeting("two", "10:30", "11:00")],
    )
    assert notify(client).status_code == 503
    assert len(mcp.calls) == 2
    assert len(processed()) == 1
    assert token_of(WORK) == START_TOKEN
    assert notify(client).status_code == 200
    # The retry dispatches the conflict that failed and nothing the first run delivered.
    assert len(mcp.calls) == 3
    delivered = [p["existing"]["event_id"] for p in mcp.payloads]
    assert sorted(delivered) == ["one", "two", "two"] or sorted(delivered) == [
        "one",
        "one",
        "two",
    ]
    assert delivered[0] != delivered[1]
    assert delivered[2] == delivered[1]
    assert len(processed()) == 2
    assert token_of(WORK) == NEXT_TOKEN


def test_a_google_failure_reading_the_overlap_answers_503_and_leaves_the_cursor(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    original = google.route

    def failing(r):
        if "timeMin" in r["query"]:
            return google_error(500)
        return original(r)

    google.route = failing
    assert notify(client).status_code in (502, 503)
    assert mcp.calls == []
    assert processed() == {}
    assert token_of(WORK) == START_TOKEN


def test_a_delta_with_no_conflict_advances_the_cursor_and_dispatches_nothing(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("later", "13:00", "14:00")],
    )
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert token_of(WORK) == NEXT_TOKEN


def test_one_dispatch_failure_does_not_block_another_calendars_notification(
    client, google, calendars, mcp
):
    mcp.fail_on = {0}
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    assert notify(client).status_code == 503
    google.script[(HOME, "home-token-0")] = Read([[]], "home-token-1")
    assert notify(client, HOME_CHANNEL).status_code == 200
    assert token_of(HOME) == "home-token-1"


# --- Issue #7: all-day busy events and cross-calendar conflicts ----------------------------


def all_day(event_id, first_day, end_day, **fields):
    return {
        "kind": "calendar#event",
        "id": event_id,
        "iCalUID": f"{event_id}@google.com",
        "status": "confirmed",
        "summary": f"Fictional {event_id}",
        "start": {"date": first_day},
        "end": {"date": end_day},
        **INSIDE,
        **fields,
    }


def test_an_all_day_busy_event_conflicts_with_a_meeting_in_its_days_and_is_all_day_in_the_payload(
    client, google, calendars, mcp
):
    world(
        google,
        [all_day("away", FUTURE, "2099-03-03")],
        [meeting("call", "10:00", "11:00")],
    )
    assert notify(client).status_code == 200
    (payload,) = mcp.payloads
    assert payload["incoming"]["all_day"] is True
    assert payload["incoming"]["start"] == f"{FUTURE}T00:00:00-05:00"
    assert payload["incoming"]["end"] == "2099-03-03T00:00:00-05:00"
    assert payload["existing"]["all_day"] is False
    assert payload["existing"]["event_id"] == "call"
    assert token_of(WORK) == NEXT_TOKEN


def test_a_meeting_changed_inside_an_all_day_event_conflicts_with_it(
    client, google, calendars, mcp
):
    world(
        google,
        [meeting("call", "10:00", "11:00")],
        [all_day("away", FUTURE, "2099-03-03")],
    )
    notify(client)
    (payload,) = mcp.payloads
    assert payload["incoming"]["event_id"] == "call"
    assert payload["existing"]["all_day"] is True
    assert payload["existing"]["start"] == f"{FUTURE}T00:00:00-05:00"
    assert payload["existing"]["end"] == "2099-03-03T00:00:00-05:00"


def test_two_all_day_events_conflict_and_an_unset_or_opaque_transparency_both_count(
    client, google, calendars, mcp
):
    world(
        google,
        [all_day("away", FUTURE, "2099-03-04", transparency="opaque")],
        [all_day("travel", "2099-03-03", "2099-03-05")],
    )
    notify(client)
    (payload,) = mcp.payloads
    assert payload["overlap"] == {
        "start": "2099-03-03T00:00:00-05:00",
        "end": "2099-03-04T00:00:00-05:00",
    }


def test_the_second_day_of_a_multi_day_event_is_covered_and_its_end_day_is_not(
    client, google, calendars, mcp
):
    away = all_day("away", FUTURE, "2099-03-04")
    world(
        google,
        [away],
        [
            meeting("second-day", "10:00", "11:00", day="2099-03-03"),
            meeting("end-day", "10:00", "11:00", day="2099-03-04"),
        ],
    )
    notify(client)
    assert [p["existing"]["event_id"] for p in mcp.payloads] == ["second-day"]


def test_an_all_day_event_with_free_transparency_raises_no_conflict_from_either_side(
    client, google, calendars, mcp
):
    free = all_day("free", FUTURE, "2099-03-03", transparency="transparent")
    call = meeting("call", "10:00", "11:00")
    world(google, [free], [call])
    assert notify(client).status_code == 200
    world(google, [call], [free], token=NEXT_TOKEN, next_token="work-token-2")
    assert notify(client).status_code == 200
    assert mcp.calls == []


def test_the_all_day_interval_is_the_primary_calendars_zone(
    client, google, calendars, mcp
):
    google.stored[WORK] = []
    google.add(
        {
            "id": WORK,
            "summary": "Fictional Work",
            "accessRole": "owner",
            "timeZone": "America/Los_Angeles",
        },
        alias="primary",
    )
    world(
        google,
        [all_day("away", FUTURE, "2099-03-03")],
        [meeting("call", "10:00", "11:00", offset="-08:00")],
    )
    notify(client)
    (payload,) = mcp.payloads
    assert payload["incoming"]["start"] == f"{FUTURE}T00:00:00-08:00"
    assert payload["incoming"]["end"] == "2099-03-03T00:00:00-08:00"


def test_an_all_day_conflict_is_recorded_once_with_the_incoming_calendar(
    client, google, calendars, mcp
):
    world(
        google,
        [all_day("away", FUTURE, "2099-03-03")],
        [meeting("call", "10:00", "11:00")],
    )
    notify(client)
    expected = prd_hash(
        (WORK, "away", utc(FUTURE, "05:00"), utc("2099-03-03", "05:00"), "internal"),
        (WORK, "call", utc(FUTURE, "15:00"), utc(FUTURE, "16:00"), "internal"),
    )
    assert {h: c for h, (c, _) in processed().items()} == {expected: WORK}


# Cross-calendar conflicts


def test_a_meeting_clashing_with_one_on_another_active_calendar_is_a_cross_calendar_conflict(
    client, google, calendars, mcp
):
    world(google, [meeting("changed", "10:00", "11:00")])
    google.stored[HOME] = [meeting("elsewhere", "10:30", "11:30")]
    assert notify(client).status_code == 200
    (payload,) = mcp.payloads
    assert payload["cross_calendar"] is True
    assert payload["policy"] == "full_evaluation"
    assert payload["incoming"]["calendar_id"] == WORK
    assert payload["existing"]["calendar_id"] == HOME
    assert payload["incoming"]["event_id"] == "changed"
    assert payload["existing"]["event_id"] == "elsewhere"


def test_both_events_of_a_cross_calendar_conflict_are_internal_whoever_is_invited(
    client, google, calendars, mcp
):
    world(google, [meeting("changed", "10:00", "11:00", **OUTSIDE)])
    google.stored[HOME] = [meeting("elsewhere", "10:30", "11:30", **OUTSIDE)]
    notify(client)
    (payload,) = mcp.payloads
    assert payload["incoming"]["classification"] == "internal"
    assert payload["existing"]["classification"] == "internal"
    assert payload["policy"] == "full_evaluation"


def test_an_all_day_event_on_another_calendar_conflicts_with_the_changed_meeting(
    client, google, calendars, mcp
):
    world(google, [meeting("changed", "10:00", "11:00")])
    google.stored[HOME] = [all_day("travel", FUTURE, "2099-03-03")]
    notify(client)
    (payload,) = mcp.payloads
    assert payload["cross_calendar"] is True
    assert payload["existing"]["all_day"] is True
    assert payload["existing"]["calendar_id"] == HOME


def test_every_active_calendar_is_read_and_a_detached_one_is_not(
    client, google, calendars, mcp
):
    world(google, [meeting("changed", "10:00", "11:00")])
    google.stored[SIDE] = [meeting("side", "10:30", "11:30")]
    notify(client)
    assert {r["path"].split("/")[4] for r in google.window_reads} == {WORK, HOME}
    assert mcp.calls == []


def test_the_window_read_of_each_calendar_covers_the_incoming_events_interval(
    client, google, calendars, mcp
):
    world(google, [meeting("changed", "10:00", "11:00")])
    notify(client)
    for read in google.window_reads:
        assert datetime.fromisoformat(
            read["query"]["timeMin"]
        ) <= datetime.fromisoformat(f"{FUTURE}T10:00:00-05:00")
        assert datetime.fromisoformat(
            read["query"]["timeMax"]
        ) >= datetime.fromisoformat(f"{FUTURE}T11:00:00-05:00")


def test_the_same_meeting_on_two_calendars_never_conflicts_with_itself(
    client, google, calendars, mcp
):
    shared = {"iCalUID": "shared@google.com"}
    world(google, [meeting("on-work", "10:00", "11:00", **shared)])
    google.stored[HOME] = [meeting("on-home", "10:00", "11:00", **shared)]
    assert notify(client).status_code == 200
    assert mcp.calls == []
    assert token_of(WORK) == NEXT_TOKEN


def test_a_cross_calendar_conflict_is_dispatched_once_when_both_calendars_notify(
    client, google, calendars, mcp
):
    first = meeting("on-work", "10:00", "11:00")
    second = meeting("on-home", "10:30", "11:30")
    world(google, [first])
    google.stored[HOME] = [second]
    google.script[(HOME, "home-token-0")] = Read([[second]], "home-token-1")
    assert notify(client).status_code == 200
    assert notify(client, HOME_CHANNEL).status_code == 200
    assert len(mcp.calls) == 1
    assert token_of(HOME) == "home-token-1"
    expected = prd_hash(
        (WORK, "on-work", utc(FUTURE, "15:00"), utc(FUTURE, "16:00"), "internal"),
        (HOME, "on-home", utc(FUTURE, "15:30"), utc(FUTURE, "16:30"), "internal"),
    )
    assert {h: c for h, (c, _) in processed().items()} == {expected: WORK}


def test_the_hash_is_the_same_whichever_calendar_found_the_conflict_first(
    client, google, calendars, mcp
):
    first = meeting("on-work", "10:00", "11:00")
    second = meeting("on-home", "10:30", "11:30")
    google.stored[WORK] = [first]
    google.stored[HOME] = [second]
    google.script[(HOME, "home-token-0")] = Read([[second]], "home-token-1")
    assert notify(client, HOME_CHANNEL).status_code == 200
    (found,) = processed()
    # The calendar recorded is the one whose delta found it.
    assert processed()[found][0] == HOME
    assert found == prd_hash(
        (WORK, "on-work", utc(FUTURE, "15:00"), utc(FUTURE, "16:00"), "internal"),
        (HOME, "on-home", utc(FUTURE, "15:30"), utc(FUTURE, "16:30"), "internal"),
    )


def test_a_delta_with_no_overlap_on_any_calendar_dispatches_nothing(
    client, google, calendars, mcp
):
    world(google, [meeting("changed", "10:00", "11:00")])
    google.stored[HOME] = [meeting("later", "11:00", "12:00")]
    assert notify(client).status_code == 200
    assert mcp.calls == []


def test_no_event_data_is_stored_in_the_database(client, google, calendars, mcp):
    world(google, [meeting("changed", "10:00", "11:00")])
    google.stored[HOME] = [meeting("elsewhere", "10:30", "11:30")]
    notify(client)
    assert mcp.calls
    stored = " ".join(
        str(cell)
        for (table,) in execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        for row in execute(f'SELECT * FROM "{table}"')
        for cell in row
    )
    for leaked in ("Fictional changed", "Fictional elsewhere", "pat@example.org"):
        assert leaked not in stored


def test_a_failed_delivery_of_a_cross_calendar_conflict_leaves_the_cursor_and_the_retry_sends_it(
    client, google, calendars, mcp
):
    world(google, [meeting("changed", "10:00", "11:00")])
    google.stored[HOME] = [meeting("elsewhere", "10:30", "11:30")]
    mcp.fail_on = {0}
    assert notify(client).status_code == 503
    assert token_of(WORK) == START_TOKEN
    assert processed() == {}
    google.script[(WORK, START_TOKEN)] = Read(
        [[meeting("changed", "10:00", "11:00")]], NEXT_TOKEN
    )
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 2
    assert len(processed()) == 1
    assert token_of(WORK) == NEXT_TOKEN
