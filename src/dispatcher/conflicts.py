"""Deciding what is a conflict and what is said about it.

Nothing here touches the network or the database. The current time and the operator's time
zone and domain arrive as arguments.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

Interval = tuple[datetime, datetime]

# Recurring Series as One Commitment, Look-ahead: how far two series are compared.
SERIES_LOOK_AHEAD = timedelta(days=90)

INTERNAL = "internal"
EXTERNAL_MIXED = "external_mixed"

# Conflict Message Contract: the instruction is the same for every conflict.
INSTRUCTION = (
    "Automated message from the calendar conflict dispatcher. Two of the operator's "
    "commitments overlap. Handle this conflict under the operator's standing guidelines "
    "for calendar conflicts and surface the outcome to the operator. Do not change either "
    "event, respond to either invitation, or contact any attendee."
)

# Conflict Message Contract: the policy for a pair of classifications, and its sentence.
IRRECONCILABLE = "irreconcilable"
NEVER_MOVE_EXTERNAL = "never_move_external"
FULL_EVALUATION = "full_evaluation"
POLICY_SENTENCES = {
    IRRECONCILABLE: (
        "Policy: Irreconcilable Conflict. Both meetings are External or Mixed. Recommend "
        "moving neither, and surface the conflict for the operator to resolve manually."
    ),
    NEVER_MOVE_EXTERNAL: (
        "Policy: never recommend moving the External or Mixed meeting. Decide whether the "
        "Internal meeting can move, or whether its inertia makes the conflict irreconcilable."
    ),
    FULL_EVALUATION: (
        "Policy: both meetings are Internal. Weigh their inertia and suggest new times for "
        "the more flexible one."
    ),
}


@dataclass(frozen=True)
class Meeting:
    calendar_id: str
    event_id: str
    ical_uid: str | None
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    classification: str
    organizer: str | None
    attendees: tuple[str, ...]
    recurrence: tuple[str, ...]
    # The series this event is an occurrence of, when it was moved or edited on its own.
    series_id: str | None = None
    # A series only: the occurrences read for the check (a conflict holds just the colliding
    # one). `start` and `end` stay the series event's own. None means none were read, and the
    # series event's own interval stands in.
    occurrences: tuple[Interval, ...] | None = None


@dataclass(frozen=True)
class Conflict:
    """One pair of overlapping meetings. `incoming` is the one the delta returned."""

    incoming: Meeting
    existing: Meeting
    overlap_start: datetime
    overlap_end: datetime

    @property
    def policy(self) -> str:
        return policy_for(self.incoming.classification, self.existing.classification)


def classify(event: dict, workspace_domain: str) -> str:
    """External / Mixed when any person taking part is outside the operator's domain.

    The organizer counts as well as the attendees, so an outside organizer who hides the guest
    list still makes the meeting External / Mixed. Rooms and equipment are not people, and a
    participant Google marks `self` is the operator's own calendar.
    """
    domain = workspace_domain.lower()
    people = [a for a in event.get("attendees", []) if not a.get("resource")]
    if "organizer" in event:
        people.append(event["organizer"])
    for person in people:
        if person.get("self"):
            continue
        _, _, person_domain = person.get("email", "").rpartition("@")
        if person_domain.lower() != domain:
            return EXTERNAL_MIXED
    return INTERNAL


def policy_for(first: str, second: str) -> str:
    if first == second == EXTERNAL_MIXED:
        return IRRECONCILABLE
    if EXTERNAL_MIXED in (first, second):
        return NEVER_MOVE_EXTERNAL
    return FULL_EVALUATION


def _moment(when: dict) -> datetime:
    moment = datetime.fromisoformat(when["dateTime"])
    # Google leaves the offset off only when it names the zone in `timeZone` instead.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(when["timeZone"]))
    return moment


def _midnight(day: str, zone: ZoneInfo) -> datetime:
    return datetime.combine(date.fromisoformat(day), time.min, tzinfo=zone)


def is_evaluable(event: dict) -> bool:
    """Whether the event can conflict at all.

    A cancelled event and a transparent one (All-Day "Busy" Event Evaluation: only
    `transparent` is excluded) are not evaluated.
    """
    return not (
        event.get("status") == "cancelled"
        or event.get("transparency") == "transparent"
        or not any(key in event.get("start", {}) for key in ("dateTime", "date"))
        or not any(key in event.get("end", {}) for key in ("dateTime", "date"))
    )


def _interval(event: dict, primary_time_zone: Callable[[], str]) -> Interval:
    """`primary_time_zone` is called only for an all-day event, which is the interval between
    two midnights of that zone (All-Day Event Arithmetic: `end.date` is exclusive)."""
    if "date" in event["start"]:
        zone = ZoneInfo(primary_time_zone())
        return (
            _midnight(event["start"]["date"], zone),
            _midnight(event["end"]["date"], zone),
        )
    return _moment(event["start"]), _moment(event["end"])


def occurrences(
    instances: list[dict],
    exception_ids: set[str],
    primary_time_zone: Callable[[], str],
) -> tuple[Interval, ...]:
    """The occurrences of a series that count as the series' own.

    `instances` is what `events.instances` returned. An occurrence moved or edited on its own
    is an event in its own right (`exception_ids`), and a cancelled one is gone (Recurring
    Series as One Commitment, Reading).
    """
    return tuple(
        _interval(instance, primary_time_zone)
        for instance in instances
        if instance.get("status") != "cancelled" and instance["id"] not in exception_ids
    )


def meeting_from_event(
    calendar_id: str,
    event: dict,
    workspace_domain: str,
    primary_time_zone: Callable[[], str],
) -> Meeting | None:
    """The event as a meeting that can conflict, or None when it can't.

    A series is one meeting, with the interval of its first occurrence.
    """
    if not is_evaluable(event):
        return None
    start, end = _interval(event, primary_time_zone)
    return Meeting(
        calendar_id=calendar_id,
        event_id=event["id"],
        ical_uid=event.get("iCalUID"),
        summary=event.get("summary", ""),
        start=start,
        end=end,
        all_day="date" in event["start"],
        classification=classify(event, workspace_domain),
        organizer=event.get("organizer", {}).get("email"),
        attendees=tuple(a["email"] for a in event.get("attendees", [])),
        recurrence=tuple(event.get("recurrence", [])),
        series_id=event.get("recurringEventId"),
    )


def find_conflicts(
    incoming: Meeting, others: list[Meeting], now: datetime
) -> list[Conflict]:
    """The conflicts between the incoming meeting and each of the others.

    Times are half-open, `[start, end)`, so meetings that only touch don't overlap. A conflict
    whose overlap has already ended is not raised, and a meeting never conflicts with itself,
    with its own series or occurrences, or with another copy of its invitation. Meetings on two
    calendars are both Internal, whoever is invited (Cross-Calendar Collision Detection).

    A series overlaps when any of its occurrences does. The pair is one conflict, which holds
    the earliest colliding occurrence of each series (Recurring Series as One Commitment).
    """
    conflicts = []
    for other in others:
        if is_itself(incoming, other):
            continue
        same_calendar = other.calendar_id == incoming.calendar_id
        collisions = [
            (max(mine[0], theirs[0]), min(mine[1], theirs[1]), mine, theirs)
            for mine in _intervals(incoming)
            for theirs in _intervals(other)
            if max(mine[0], theirs[0]) < min(mine[1], theirs[1])
            and min(mine[1], theirs[1]) > now
        ]
        if not collisions:
            continue
        overlap_start, overlap_end, mine, theirs = min(
            collisions, key=lambda collision: collision[0]
        )
        first, second = _at(incoming, mine), _at(other, theirs)
        if not same_calendar:
            first = replace(first, classification=INTERNAL)
            second = replace(second, classification=INTERNAL)
        conflicts.append(Conflict(first, second, overlap_start, overlap_end))
    return conflicts


def is_itself(incoming: Meeting, other: Meeting) -> bool:
    """Whether the two are one commitment: the same event, a series and one of its
    occurrences, or copies of one invitation."""
    same_calendar = other.calendar_id == incoming.calendar_id
    return bool(
        (
            same_calendar
            and (
                other.event_id == incoming.event_id
                or other.series_id == incoming.event_id
                or incoming.series_id == other.event_id
            )
        )
        or (incoming.ical_uid and other.ical_uid == incoming.ical_uid)
    )


def look_ahead(incoming: Meeting, other: Meeting, now: datetime) -> Interval:
    """Recurring Series as One Commitment, Look-ahead: the span whose occurrences are
    examined when at least one of the two is a series."""
    if incoming.recurrence and other.recurrence:
        return now, now + SERIES_LOOK_AHEAD
    single = other if incoming.recurrence else incoming
    return single.start, single.end


def _intervals(meeting: Meeting) -> tuple[Interval, ...]:
    if meeting.occurrences is None:
        return ((meeting.start, meeting.end),)
    return meeting.occurrences


def _at(meeting: Meeting, interval: Interval) -> Meeting:
    return replace(meeting, occurrences=(interval,)) if meeting.recurrence else meeting


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_hash(conflict: Conflict) -> str:
    """Conflict De-duplication: the same hash whichever of the two meetings was incoming."""
    meetings = sorted(
        (conflict.incoming, conflict.existing),
        key=lambda meeting: (meeting.calendar_id, meeting.event_id),
    )
    fields = [
        field
        for meeting in meetings
        for field in (
            meeting.calendar_id,
            meeting.event_id,
            _utc(meeting.start),
            _utc(meeting.end),
            " ".join(meeting.recurrence),
            meeting.classification,
        )
    ]
    return hashlib.sha256("|".join(fields).encode()).hexdigest()


def _rfc3339(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).isoformat(timespec="seconds")


def _meeting_payload(meeting: Meeting, zone: ZoneInfo) -> dict:
    # The colliding occurrence of a series, not its first one (Conflict Message Contract).
    start, end = _intervals(meeting)[0]
    # No description, location, conference link, or attachment: an invitation's text is written
    # by whoever sent it (Conflict Message Contract).
    return {
        "calendar_id": meeting.calendar_id,
        "event_id": meeting.event_id,
        "summary": meeting.summary,
        "start": _rfc3339(start, zone),
        "end": _rfc3339(end, zone),
        "all_day": meeting.all_day,
        "recurring": bool(meeting.recurrence),
        "classification": meeting.classification,
        "organizer": meeting.organizer,
        "attendees": list(meeting.attendees),
    }


def message(conflict: Conflict, time_zone: str) -> str:
    """The text of the `ask_sentience` call: instruction, policy sentence, fenced JSON payload."""
    zone = ZoneInfo(time_zone)
    payload = {
        "policy": conflict.policy,
        "cross_calendar": conflict.incoming.calendar_id
        != conflict.existing.calendar_id,
        "overlap": {
            "start": _rfc3339(conflict.overlap_start, zone),
            "end": _rfc3339(conflict.overlap_end, zone),
        },
        "incoming": _meeting_payload(conflict.incoming, zone),
        "existing": _meeting_payload(conflict.existing, zone),
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    return (
        f"{INSTRUCTION}\n\n{POLICY_SENTENCES[conflict.policy]}\n\n```json\n{body}\n```"
    )
