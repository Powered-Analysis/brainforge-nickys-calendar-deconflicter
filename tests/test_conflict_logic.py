"""Issue #6: the I/O-free logic that decides what is a conflict and what is said about it.

Every expectation comes from the issue's acceptance criteria and the PRD's Attendee Domain
Classification, Deterministic Dispatch Rules, Conflict Message Contract, and Conflict
De-duplication. The current time, the operator's domain, and the primary time zone are
arguments, so nothing here touches the network or the database.
"""

import hashlib
import json
from datetime import UTC, datetime

import pytest

from dispatcher import conflicts

DOMAIN = "example.com"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
ZONE = "America/New_York"

# Conflict Message Contract, verbatim.
PRD_INSTRUCTION = (
    "Automated message from the calendar conflict dispatcher. Two of the operator's "
    "commitments overlap. Handle this conflict under the operator's standing guidelines "
    "for calendar conflicts and surface the outcome to the operator. Do not change either "
    "event, respond to either invitation, or contact any attendee."
)
PRD_SENTENCES = {
    "irreconcilable": "Policy: Irreconcilable Conflict. Both meetings are External or Mixed. Recommend moving neither, and surface the conflict for the operator to resolve manually.",
    "never_move_external": "Policy: never recommend moving the External or Mixed meeting. Decide whether the Internal meeting can move, or whether its inertia makes the conflict irreconcilable.",
    "full_evaluation": "Policy: both meetings are Internal. Weigh their inertia and suggest new times for the more flexible one.",
}
PRD_HASH_STRING = (
    "operator@example.com|4kq1t8f0example|2026-03-02T15:00:00Z|2026-03-02T16:00:00Z||external_mixed|"
    "operator@example.com|7hd02mzexample|2026-03-02T14:30:00Z|2026-03-02T15:30:00Z||internal"
)
PRD_HASH = "fe51fe1ff6c549b16f83b3c01c01ca3652734fb3dfc27cf34edb6bf25fbc143d"


def person(email, **flags):
    return {"email": email, **flags}


def event(event_id, start, end, **fields):
    return {
        "id": event_id,
        "iCalUID": f"{event_id}@google.com",
        "status": "confirmed",
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        **fields,
    }


def incoming_event():
    return event(
        "4kq1t8f0example",
        "2026-03-02T10:00:00-05:00",
        "2026-03-02T11:00:00-05:00",
        summary="Vendor review",
        organizer=person("pat@example.org"),
        attendees=[person("operator@example.com"), person("pat@example.org")],
    )


def existing_event():
    return event(
        "7hd02mzexample",
        "2026-03-02T09:30:00-05:00",
        "2026-03-02T10:30:00-05:00",
        summary="Team sync",
        organizer=person("operator@example.com"),
        attendees=[person("operator@example.com"), person("sam@example.com")],
    )


def meeting(raw, calendar_id="operator@example.com"):
    result = conflicts.meeting_from_event(calendar_id, raw, DOMAIN, lambda: ZONE)
    assert result is not None
    return result


def conflict_of(first, second, now=NOW):
    found = conflicts.find_conflicts(meeting(first), [meeting(second)], now)
    assert len(found) == 1
    return found[0]


def prd_conflict():
    return conflict_of(incoming_event(), existing_event())


# --- Classification (criterion 5) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {"attendees": [person("operator@example.com"), person("sam@example.com")]},
            "internal",
        ),
        (
            {"attendees": [person("operator@example.com"), person("pat@example.org")]},
            "external_mixed",
        ),
        ({"attendees": [person("Sam@Example.COM")]}, "internal"),
        ({"attendees": [person("lee@eng.example.com")]}, "external_mixed"),
        (
            {
                "attendees": [
                    person("operator@example.com"),
                    person("room-1@resource.calendar.google.com", resource=True),
                ]
            },
            "internal",
        ),
        ({"organizer": person("operator@example.com")}, "internal"),
        (
            {
                "organizer": person("pat@example.org"),
                "attendees": [person("operator@example.com")],
            },
            "external_mixed",
        ),
        (
            {
                "organizer": person("team-cal@group.calendar.google.com", self=True),
                "attendees": [person("sam@example.com")],
            },
            "internal",
        ),
        ({}, "internal"),
        ({"attendees": [person("operator@example.com", self=True)]}, "internal"),
        (
            {"attendees": [person("pat@example.org", responseStatus="declined")]},
            "external_mixed",
        ),
        ({"attendees": [person("sam@notexample.com")]}, "external_mixed"),
        ({"attendees": [person("sam@example.com.evil.org")]}, "external_mixed"),
    ],
    ids=[
        "all-inside",
        "one-outside",
        "case-insensitive",
        "subdomain-is-outside",
        "room-does-not-count",
        "organizer-only-inside",
        "outside-organizer-hidden-guests",
        "self-organizer-on-secondary-calendar",
        "no-participants",
        "self-attendee",
        "declined-outsider-still-counts",
        "suffix-lookalike-is-outside",
        "prefix-lookalike-is-outside",
    ],
)
def test_classification_follows_the_participants_domains(raw, expected):
    assert conflicts.classify(raw, DOMAIN) == expected


def test_the_workspace_domain_is_compared_case_insensitively():
    raw = {"attendees": [person("sam@example.com")]}
    assert conflicts.classify(raw, "Example.COM") == "internal"


# --- Policy (criterion 6) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "policy"),
    [
        ("external_mixed", "external_mixed", "irreconcilable"),
        ("external_mixed", "internal", "never_move_external"),
        ("internal", "external_mixed", "never_move_external"),
        ("internal", "internal", "full_evaluation"),
    ],
)
def test_policy_follows_the_two_classifications(first, second, policy):
    assert conflicts.policy_for(first, second) == policy


# --- Exclusions (criterion 1) --------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "cancelled"},
        {"transparency": "transparent"},
        {
            "start": {"date": "2026-03-02"},
            "end": {"date": "2026-03-03"},
            "transparency": "transparent",
        },
    ],
    ids=["cancelled", "transparent", "all-day-transparent"],
)
def test_an_excluded_event_is_not_a_meeting(fields):
    raw = {**existing_event(), **fields}
    assert (
        conflicts.meeting_from_event("operator@example.com", raw, DOMAIN, lambda: ZONE)
        is None
    )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"transparency": "opaque"},
        {
            "recurringEventId": "series-example",
            "originalStartTime": {"dateTime": "2026-03-02T09:30:00-05:00"},
        },
    ],
    ids=["transparency-unset", "opaque", "moved-occurrence"],
)
def test_an_ordinary_event_is_a_meeting(fields):
    raw = {**existing_event(), **fields}
    assert (
        conflicts.meeting_from_event("operator@example.com", raw, DOMAIN, lambda: ZONE)
        is not None
    )


# --- Overlap (criteria 2, 3, 4, 7) ---------------------------------------------------------


def timed(event_id, start, end, **fields):
    return event(event_id, f"2026-03-02T{start}:00Z", f"2026-03-02T{end}:00Z", **fields)


def test_overlapping_meetings_conflict_from_the_later_start_to_the_earlier_end():
    found = conflict_of(timed("a", "10:00", "11:00"), timed("b", "10:30", "11:30"))
    assert found.overlap_start == datetime(2026, 3, 2, 10, 30, tzinfo=UTC)
    assert found.overlap_end == datetime(2026, 3, 2, 11, 0, tzinfo=UTC)


def test_the_overlap_is_the_same_from_either_side():
    found = conflict_of(timed("b", "10:30", "11:30"), timed("a", "10:00", "11:00"))
    assert found.overlap_start == datetime(2026, 3, 2, 10, 30, tzinfo=UTC)
    assert found.overlap_end == datetime(2026, 3, 2, 11, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("other_start", "other_end"),
    [("11:00", "12:00"), ("09:00", "10:00")],
    ids=["starts-as-it-ends", "ends-as-it-starts"],
)
def test_meetings_that_only_touch_do_not_conflict(other_start, other_end):
    first = meeting(timed("a", "10:00", "11:00"))
    other = meeting(timed("b", other_start, other_end))
    assert conflicts.find_conflicts(first, [other], NOW) == []


def test_a_contained_meeting_conflicts_over_its_whole_length():
    found = conflict_of(timed("a", "10:00", "12:00"), timed("b", "10:30", "11:00"))
    assert found.overlap_start == datetime(2026, 3, 2, 10, 30, tzinfo=UTC)
    assert found.overlap_end == datetime(2026, 3, 2, 11, 0, tzinfo=UTC)


def test_an_identical_interval_conflicts():
    conflict_of(timed("a", "10:00", "11:00"), timed("b", "10:00", "11:00"))


def test_a_meeting_overlapping_three_others_gives_three_conflicts_with_it_incoming():
    first = meeting(timed("a", "10:00", "12:00"))
    others = [
        meeting(timed("b", "09:00", "10:30")),
        meeting(timed("c", "10:30", "11:00")),
        meeting(timed("d", "11:30", "13:00")),
    ]
    found = conflicts.find_conflicts(first, others, NOW)
    assert [c.existing.event_id for c in found] == ["b", "c", "d"]
    assert {c.incoming.event_id for c in found} == {"a"}


def test_a_conflict_whose_overlap_has_ended_is_not_raised():
    first = meeting(timed("a", "10:00", "11:00"))
    other = meeting(timed("b", "10:30", "11:30"))
    after = datetime(2026, 3, 2, 11, 0, tzinfo=UTC)
    assert conflicts.find_conflicts(first, [other], after) == []
    assert (
        conflicts.find_conflicts(first, [other], datetime(2026, 3, 2, 12, tzinfo=UTC))
        == []
    )


def test_a_conflict_whose_overlap_is_still_ahead_is_raised_even_when_the_meetings_began():
    first = meeting(timed("a", "10:00", "11:00"))
    other = meeting(timed("b", "10:30", "11:30"))
    during = datetime(2026, 3, 2, 10, 45, tzinfo=UTC)
    assert len(conflicts.find_conflicts(first, [other], during)) == 1


def test_an_overlap_ending_exactly_now_is_not_raised():
    first = meeting(timed("a", "10:00", "11:00"))
    other = meeting(timed("b", "10:30", "11:30"))
    assert (
        conflicts.find_conflicts(
            first, [other], datetime(2026, 3, 2, 11, 0, tzinfo=UTC)
        )
        == []
    )
    assert (
        len(
            conflicts.find_conflicts(
                first, [other], datetime(2026, 3, 2, 10, 59, 59, tzinfo=UTC)
            )
        )
        == 1
    )


def test_a_meeting_never_conflicts_with_itself():
    first = meeting(timed("a", "10:00", "11:00"))
    assert conflicts.find_conflicts(first, [first], NOW) == []


def test_a_meeting_never_conflicts_with_another_copy_of_its_invitation():
    first = meeting(
        timed("a", "10:00", "11:00", iCalUID="shared-invitation@google.com")
    )
    copy = meeting(timed("b", "10:30", "11:30", iCalUID="shared-invitation@google.com"))
    assert conflicts.find_conflicts(first, [copy], NOW) == []


def test_times_in_different_offsets_are_compared_as_instants():
    first = meeting(
        event("a", "2026-03-02T10:00:00-05:00", "2026-03-02T11:00:00-05:00")
    )
    touching = meeting(
        event("b", "2026-03-02T17:00:00+01:00", "2026-03-02T18:00:00+01:00")
    )
    overlapping = meeting(
        event("c", "2026-03-02T16:30:00+01:00", "2026-03-02T17:30:00+01:00")
    )
    assert conflicts.find_conflicts(first, [touching], NOW) == []
    assert len(conflicts.find_conflicts(first, [overlapping], NOW)) == 1


def test_the_conflicts_policy_follows_its_meetings_classifications():
    assert prd_conflict().policy == "never_move_external"


# --- Conflict message (criteria 8 and 9) ---------------------------------------------------


def parts_of(text):
    instruction, sentence, block = text.split("\n\n", 2)
    return instruction, sentence, block


def payload_of(text):
    _, _, block = parts_of(text)
    assert block.startswith("```json\n")
    assert block.endswith("\n```")
    return json.loads(block.removeprefix("```json\n").removesuffix("\n```"))


PRD_PAYLOAD = {
    "policy": "never_move_external",
    "cross_calendar": False,
    "overlap": {
        "start": "2026-03-02T10:00:00-05:00",
        "end": "2026-03-02T10:30:00-05:00",
    },
    "incoming": {
        "calendar_id": "operator@example.com",
        "event_id": "4kq1t8f0example",
        "summary": "Vendor review",
        "start": "2026-03-02T10:00:00-05:00",
        "end": "2026-03-02T11:00:00-05:00",
        "all_day": False,
        "recurring": False,
        "classification": "external_mixed",
        "organizer": "pat@example.org",
        "attendees": ["operator@example.com", "pat@example.org"],
    },
    "existing": {
        "calendar_id": "operator@example.com",
        "event_id": "7hd02mzexample",
        "summary": "Team sync",
        "start": "2026-03-02T09:30:00-05:00",
        "end": "2026-03-02T10:30:00-05:00",
        "all_day": False,
        "recurring": False,
        "classification": "internal",
        "organizer": "operator@example.com",
        "attendees": ["operator@example.com", "sam@example.com"],
    },
}


def test_the_prd_example_gives_exactly_the_prd_payload():
    assert payload_of(conflicts.message(prd_conflict(), ZONE)) == PRD_PAYLOAD


def test_the_payload_keys_are_exactly_the_prds_fields_in_the_prds_order():
    payload = payload_of(conflicts.message(prd_conflict(), ZONE))
    assert list(payload) == [
        "policy",
        "cross_calendar",
        "overlap",
        "incoming",
        "existing",
    ]
    assert list(payload["overlap"]) == ["start", "end"]
    meeting_keys = [
        "calendar_id", "event_id", "summary", "start", "end", "all_day",
        "recurring", "classification", "organizer", "attendees",
    ]  # fmt: skip
    assert list(payload["incoming"]) == meeting_keys
    assert list(payload["existing"]) == meeting_keys


def test_the_message_is_instruction_sentence_and_fenced_payload_separated_by_blank_lines():
    text = conflicts.message(prd_conflict(), ZONE)
    instruction, sentence, block = parts_of(text)
    assert instruction == PRD_INSTRUCTION
    assert sentence == PRD_SENTENCES["never_move_external"]
    assert block.startswith("```json\n")
    assert block.endswith("\n```")
    assert block.count("```") == 2


@pytest.mark.parametrize("policy", sorted(PRD_SENTENCES))
def test_each_policys_sentence_is_verbatim(policy):
    internal = {"organizer": person("operator@example.com")}
    external = {"organizer": person("pat@example.org")}
    sides = {
        "irreconcilable": (external, external),
        "never_move_external": (external, internal),
        "full_evaluation": (internal, internal),
    }[policy]
    first = timed("a", "10:00", "11:00", **sides[0])
    second = timed("b", "10:30", "11:30", **sides[1])
    text = conflicts.message(conflict_of(first, second), ZONE)
    assert parts_of(text)[1] == PRD_SENTENCES[policy]
    assert payload_of(text)["policy"] == policy


def test_timestamps_carry_the_primary_zones_offset_at_that_instant_through_daylight_saving():
    winter = timed("a", "15:00", "16:00")
    winter_other = timed("b", "15:30", "16:30")
    summer = event("c", "2026-07-01T15:00:00Z", "2026-07-01T16:00:00Z")
    summer_other = event("d", "2026-07-01T15:30:00Z", "2026-07-01T16:30:00Z")
    winter_payload = payload_of(
        conflicts.message(conflict_of(winter, winter_other), ZONE)
    )
    summer_payload = payload_of(
        conflicts.message(conflict_of(summer, summer_other), ZONE)
    )
    assert winter_payload["incoming"]["start"] == "2026-03-02T10:00:00-05:00"
    assert winter_payload["overlap"]["end"] == "2026-03-02T11:00:00-05:00"
    assert summer_payload["incoming"]["start"] == "2026-07-01T11:00:00-04:00"
    assert summer_payload["overlap"]["start"] == "2026-07-01T11:30:00-04:00"


def test_timestamps_use_the_primary_zone_whatever_zone_the_event_was_written_in():
    first = event("a", "2026-03-02T16:00:00+01:00", "2026-03-02T17:00:00+01:00")
    second = event("b", "2026-03-02T16:30:00+01:00", "2026-03-02T17:30:00+01:00")
    payload = payload_of(conflicts.message(conflict_of(first, second), "Asia/Tokyo"))
    assert payload["incoming"]["start"] == "2026-03-03T00:00:00+09:00"


def test_a_missing_title_is_empty_and_a_missing_organizer_is_null_and_no_attendees_is_an_empty_list():
    bare_first = timed("a", "10:00", "11:00")
    bare_second = timed("b", "10:30", "11:30")
    payload = payload_of(conflicts.message(conflict_of(bare_first, bare_second), ZONE))
    for side in ("incoming", "existing"):
        assert payload[side]["summary"] == ""
        assert payload[side]["organizer"] is None
        assert payload[side]["attendees"] == []


def test_attendees_keep_googles_order_and_include_resources():
    first = timed(
        "a", "10:00", "11:00",
        attendees=[
            person("zed@example.com"),
            person("room-9@resource.calendar.google.com", resource=True),
            person("amy@example.com"),
        ],
    )  # fmt: skip
    payload = payload_of(
        conflicts.message(conflict_of(first, timed("b", "10:30", "11:30")), ZONE)
    )
    assert payload["incoming"]["attendees"] == [
        "zed@example.com",
        "room-9@resource.calendar.google.com",
        "amy@example.com",
    ]


def test_nothing_but_the_identifiers_and_the_title_is_sent():
    secret = timed(
        "a", "10:00", "11:00",
        summary="Fictional title",
        description="DESCRIPTION-MUST-NOT-LEAK",
        location="LOCATION-MUST-NOT-LEAK",
        hangoutLink="https://meet.example.com/CONFERENCE-MUST-NOT-LEAK",
        conferenceData={"entryPoints": [{"uri": "https://meet.example.com/CONFERENCE2"}]},
        attachments=[{"fileUrl": "https://files.example.com/ATTACHMENT-MUST-NOT-LEAK"}],
    )  # fmt: skip
    text = conflicts.message(conflict_of(secret, timed("b", "10:30", "11:30")), ZONE)
    for leaked in ("MUST-NOT-LEAK", "CONFERENCE2", "meet.example.com"):
        assert leaked not in text


def test_a_title_with_quotes_fences_and_non_ascii_text_still_gives_a_parsable_single_block():
    title = 'Review "Q1" ``` plan — café'
    first = timed("a", "10:00", "11:00", summary=title)
    text = conflicts.message(conflict_of(first, timed("b", "10:30", "11:30")), ZONE)
    assert text.endswith("\n```")
    assert payload_of(text)["incoming"]["summary"] == title


def test_cross_calendar_is_false_and_all_day_and_recurring_are_false_for_one_calendar():
    payload = payload_of(conflicts.message(prd_conflict(), ZONE))
    assert payload["cross_calendar"] is False
    for side in ("incoming", "existing"):
        assert payload[side]["all_day"] is False
        assert payload[side]["recurring"] is False


# --- event_hash (criteria 10 and 15) -------------------------------------------------------


def test_the_prds_string_hashes_to_the_prds_hash():
    # Guards the test's own constants.
    assert hashlib.sha256(PRD_HASH_STRING.encode()).hexdigest() == PRD_HASH


def test_the_prd_example_gives_the_prds_hash():
    assert conflicts.event_hash(prd_conflict()) == PRD_HASH


def test_the_hash_does_not_depend_on_which_meeting_was_incoming():
    swapped = conflict_of(existing_event(), incoming_event())
    assert swapped.incoming.event_id == "7hd02mzexample"
    assert conflicts.event_hash(swapped) == PRD_HASH


def test_a_title_change_does_not_change_the_hash():
    retitled = {**incoming_event(), "summary": "A different title"}
    assert conflicts.event_hash(conflict_of(retitled, existing_event())) == PRD_HASH


def test_an_attendee_change_that_keeps_the_classification_does_not_change_the_hash():
    changed = {
        **existing_event(),
        "attendees": [person("operator@example.com"), person("kim@example.com")],
    }
    assert conflicts.event_hash(conflict_of(incoming_event(), changed)) == PRD_HASH


def test_an_added_room_does_not_change_the_hash():
    changed = {
        **existing_event(),
        "attendees": [
            *existing_event()["attendees"],
            person("room@resource.calendar.google.com", resource=True),
        ],
    }
    assert conflicts.event_hash(conflict_of(incoming_event(), changed)) == PRD_HASH


def test_the_same_instants_written_in_another_offset_give_the_same_hash():
    rewritten = {
        **incoming_event(),
        "start": {"dateTime": "2026-03-02T15:00:00Z"},
        "end": {"dateTime": "2026-03-02T16:00:00Z"},
    }
    assert conflicts.event_hash(conflict_of(rewritten, existing_event())) == PRD_HASH


@pytest.mark.parametrize(
    "change",
    [
        {"start": {"dateTime": "2026-03-02T09:45:00-05:00"}},
        {"end": {"dateTime": "2026-03-02T11:30:00-05:00"}},
    ],
    ids=["start-moves", "end-moves"],
)
def test_a_time_change_to_either_meeting_gives_a_new_hash(change):
    for changed_incoming, changed_existing in (
        ({**incoming_event(), **change}, existing_event()),
        (incoming_event(), {**existing_event(), **change}),
    ):
        found = conflicts.find_conflicts(
            meeting(changed_incoming), [meeting(changed_existing)], NOW
        )
        assert len(found) == 1
        assert conflicts.event_hash(found[0]) != PRD_HASH


def test_a_classification_change_gives_a_new_hash():
    now_internal = {
        **incoming_event(),
        "organizer": person("operator@example.com"),
        "attendees": [person("operator@example.com"), person("sam@example.com")],
    }
    assert conflicts.event_hash(conflict_of(now_internal, existing_event())) != PRD_HASH


def test_the_hash_is_lowercase_hex_sha256():
    value = conflicts.event_hash(prd_conflict())
    assert len(value) == 64
    assert value == value.lower()
    int(value, 16)


def test_the_two_meetings_are_ordered_by_calendar_id_then_event_id():
    first = meeting(incoming_event(), "b-calendar@example.com")
    second = meeting(existing_event(), "a-calendar@example.com")
    found = conflicts.find_conflicts(first, [second], NOW)[0]
    expected = (
        "a-calendar@example.com|7hd02mzexample|2026-03-02T14:30:00Z|2026-03-02T15:30:00Z||internal|"
        "b-calendar@example.com|4kq1t8f0example|2026-03-02T15:00:00Z|2026-03-02T16:00:00Z||internal"
    )
    assert conflicts.event_hash(found) == hashlib.sha256(expected.encode()).hexdigest()


# --- Issue #7: all-day events and cross-calendar conflicts ---------------------------------

LA = "America/Los_Angeles"
HOME_CALENDAR = "home@example.com"


def all_day(event_id, first_day, end_day, **fields):
    """An all-day event: `end.date` is the day after its last day."""
    return {
        "id": event_id,
        "iCalUID": f"{event_id}@google.com",
        "status": "confirmed",
        "start": {"date": first_day},
        "end": {"date": end_day},
        "summary": f"Fictional {event_id}",
        **fields,
    }


def all_day_meeting(raw, zone=ZONE, calendar_id="operator@example.com"):
    result = conflicts.meeting_from_event(calendar_id, raw, DOMAIN, lambda: zone)
    assert result is not None
    return result


def local(event_id, start, end, **fields):
    """A timed event on 2026-03-02 in the primary zone's offset."""
    return event(
        event_id,
        f"2026-03-02T{start}:00-05:00",
        f"2026-03-02T{end}:00-05:00",
        **fields,
    )


def stamps(meeting_):
    return meeting_.start.isoformat(), meeting_.end.isoformat()


@pytest.mark.parametrize(
    ("first_day", "end_day", "start", "end"),
    [
        (
            "2026-03-02",
            "2026-03-03",
            "2026-03-02T00:00:00-05:00",
            "2026-03-03T00:00:00-05:00",
        ),
        (
            # Daylight saving time begins: the day is 23 hours long.
            "2026-03-08",
            "2026-03-09",
            "2026-03-08T00:00:00-05:00",
            "2026-03-09T00:00:00-04:00",
        ),
        (
            # A multi-day event spans every day in the range, `end.date` excluded.
            "2026-03-02",
            "2026-03-05",
            "2026-03-02T00:00:00-05:00",
            "2026-03-05T00:00:00-05:00",
        ),
    ],
    ids=["one-day", "dst-start", "multi-day"],
)
def test_an_all_day_event_is_midnight_to_midnight_in_the_primary_zone(
    first_day, end_day, start, end
):
    found = all_day_meeting(all_day("away", first_day, end_day))
    assert stamps(found) == (start, end)
    assert found.all_day is True


@pytest.mark.parametrize(
    "fields", [{}, {"transparency": "opaque"}], ids=["unset", "opaque"]
)
def test_an_opaque_or_unset_all_day_event_is_a_meeting(fields):
    assert all_day_meeting(all_day("away", "2026-03-02", "2026-03-03", **fields))


def test_a_transparent_all_day_event_is_not_a_meeting():
    raw = all_day("free", "2026-03-02", "2026-03-03", transparency="transparent")
    assert (
        conflicts.meeting_from_event("x@example.com", raw, DOMAIN, lambda: ZONE) is None
    )


def test_the_all_day_interval_uses_the_zone_given_not_the_events_or_a_fixed_one():
    raw = all_day("away", "2026-03-02", "2026-03-03")
    assert stamps(all_day_meeting(raw, LA)) == (
        "2026-03-02T00:00:00-08:00",
        "2026-03-03T00:00:00-08:00",
    )


def test_a_timed_event_does_not_depend_on_the_primary_zone():
    # The zone is only a way to ask; a timed event never needs it.
    def no_zone():
        raise AssertionError("asked for the zone for a timed event")

    assert conflicts.meeting_from_event(
        "x@example.com", existing_event(), DOMAIN, no_zone
    )


def test_an_all_day_event_conflicts_with_a_timed_meeting_either_way_round():
    away = all_day("away", "2026-03-02", "2026-03-03")
    timed_ = local("call", "10:00", "11:00")
    for first, second in ((away, timed_), (timed_, away)):
        found = conflicts.find_conflicts(
            all_day_meeting(first), [all_day_meeting(second)], NOW
        )
        assert len(found) == 1
        assert found[0].overlap_start.isoformat() == "2026-03-02T10:00:00-05:00"
        assert found[0].overlap_end.isoformat() == "2026-03-02T11:00:00-05:00"


def test_two_all_day_events_conflict_over_the_days_they_share():
    first = all_day_meeting(all_day("a", "2026-03-02", "2026-03-05"))
    second = all_day_meeting(all_day("b", "2026-03-04", "2026-03-07"))
    found = conflicts.find_conflicts(first, [second], NOW)
    assert len(found) == 1
    assert found[0].overlap_start.isoformat() == "2026-03-04T00:00:00-05:00"
    assert found[0].overlap_end.isoformat() == "2026-03-05T00:00:00-05:00"


def test_consecutive_all_day_events_do_not_conflict():
    first = all_day_meeting(all_day("a", "2026-03-02", "2026-03-03"))
    second = all_day_meeting(all_day("b", "2026-03-03", "2026-03-04"))
    assert conflicts.find_conflicts(first, [second], NOW) == []


def test_a_meeting_at_midnight_ending_the_all_day_event_does_not_conflict():
    away = all_day_meeting(all_day("away", "2026-03-02", "2026-03-03"))
    # The end is excluded: a meeting starting at 00:00 on `end.date` is outside it.
    after = conflicts.meeting_from_event(
        "operator@example.com",
        event("late", "2026-03-03T00:00:00-05:00", "2026-03-03T01:00:00-05:00"),
        DOMAIN,
        lambda: ZONE,
    )
    assert conflicts.find_conflicts(away, [after], NOW) == []


def test_an_all_day_conflict_whose_days_are_over_is_not_raised():
    away = all_day_meeting(all_day("away", "2026-02-20", "2026-02-21"))
    other = all_day_meeting(all_day("b", "2026-02-20", "2026-02-21"))
    assert conflicts.find_conflicts(away, [other], NOW) == []


def test_an_all_day_meetings_payload_entry_is_all_day_with_the_interval_as_start_and_end():
    away = all_day_meeting(all_day("away", "2026-03-08", "2026-03-09"))
    other = meeting(
        event("call", "2026-03-08T10:00:00-04:00", "2026-03-08T11:00:00-04:00")
    )
    found = conflicts.find_conflicts(away, [other], NOW)[0]
    payload = payload_of(conflicts.message(found, ZONE))
    assert payload["incoming"]["all_day"] is True
    assert payload["incoming"]["start"] == "2026-03-08T00:00:00-05:00"
    assert payload["incoming"]["end"] == "2026-03-09T00:00:00-04:00"
    assert payload["existing"]["all_day"] is False


def test_an_all_day_conflicts_hash_uses_the_intervals_instants():
    away = all_day_meeting(all_day("away", "2026-03-02", "2026-03-03"))
    other = meeting(local("call", "10:00", "11:00"))
    found = conflicts.find_conflicts(away, [other], NOW)[0]
    first, second = sorted(
        [
            (
                "operator@example.com",
                "away",
                "2026-03-02T05:00:00Z",
                "2026-03-03T05:00:00Z",
            ),
            (
                "operator@example.com",
                "call",
                "2026-03-02T15:00:00Z",
                "2026-03-02T16:00:00Z",
            ),
        ]
    )
    expected = "|".join([*first, "", "internal", *second, "", "internal"])
    assert conflicts.event_hash(found) == hashlib.sha256(expected.encode()).hexdigest()


# Cross-Calendar Collision Detection and the Message Contract's policy table.


def on_home(raw):
    return meeting(raw, HOME_CALENDAR)


def test_a_conflict_across_two_calendars_is_cross_calendar_internal_and_full_evaluation():
    outside = {
        "organizer": person("pat@example.org"),
        "attendees": [person("operator@example.com"), person("pat@example.org")],
    }
    here = meeting(local("a", "10:00", "11:00", **outside))
    there = on_home(local("b", "10:30", "11:30", **outside))
    found = conflicts.find_conflicts(here, [there], NOW)
    assert len(found) == 1
    assert found[0].incoming.classification == "internal"
    assert found[0].existing.classification == "internal"
    assert found[0].policy == "full_evaluation"
    payload = payload_of(conflicts.message(found[0], ZONE))
    assert payload["cross_calendar"] is True
    assert payload["policy"] == "full_evaluation"
    assert payload["incoming"]["classification"] == "internal"
    assert payload["existing"]["classification"] == "internal"
    assert payload["incoming"]["calendar_id"] == "operator@example.com"
    assert payload["existing"]["calendar_id"] == HOME_CALENDAR


def test_a_conflict_on_one_calendar_keeps_its_classifications_and_is_not_cross_calendar():
    found = conflict_of(incoming_event(), existing_event())
    assert found.policy == "never_move_external"
    assert payload_of(conflicts.message(found, ZONE))["cross_calendar"] is False


def test_an_all_day_event_on_another_calendar_conflicts_and_is_internal():
    away = all_day_meeting(
        all_day("away", "2026-03-02", "2026-03-03"), calendar_id=HOME_CALENDAR
    )
    call = meeting(incoming_event())
    found = conflicts.find_conflicts(call, [away], NOW)
    assert len(found) == 1
    assert {found[0].incoming.classification, found[0].existing.classification} == {
        "internal"
    }
    assert payload_of(conflicts.message(found[0], ZONE))["cross_calendar"] is True


def test_the_same_meeting_on_two_calendars_never_conflicts_with_itself():
    raw = local("shared", "10:00", "11:00", iCalUID="shared-uid@google.com")
    assert conflicts.find_conflicts(meeting(raw), [on_home(raw)], NOW) == []


def test_the_same_all_day_event_on_two_calendars_never_conflicts_with_itself():
    raw = all_day("away", "2026-03-02", "2026-03-03", iCalUID="shared-uid@google.com")
    here = all_day_meeting(raw)
    there = all_day_meeting(raw, calendar_id=HOME_CALENDAR)
    assert conflicts.find_conflicts(here, [there], NOW) == []


def test_a_cross_calendar_conflict_has_the_same_hash_whichever_side_was_incoming():
    one = meeting(local("a", "10:00", "11:00"))
    two = on_home(local("b", "10:30", "11:30"))
    from_one = conflicts.find_conflicts(one, [two], NOW)[0]
    from_two = conflicts.find_conflicts(two, [one], NOW)[0]
    assert conflicts.event_hash(from_one) == conflicts.event_hash(from_two)


def test_the_hash_of_a_cross_calendar_conflict_carries_both_events_as_internal():
    outside = {"organizer": person("pat@example.org")}
    one = meeting(local("a", "10:00", "11:00", **outside))
    two = on_home(local("b", "10:30", "11:30"))
    found = conflicts.find_conflicts(one, [two], NOW)[0]
    first, second = sorted(
        [
            (
                "operator@example.com",
                "a",
                "2026-03-02T15:00:00Z",
                "2026-03-02T16:00:00Z",
            ),
            (HOME_CALENDAR, "b", "2026-03-02T15:30:00Z", "2026-03-02T16:30:00Z"),
        ]
    )
    expected = "|".join([*first, "", "internal", *second, "", "internal"])
    assert conflicts.event_hash(found) == hashlib.sha256(expected.encode()).hexdigest()
