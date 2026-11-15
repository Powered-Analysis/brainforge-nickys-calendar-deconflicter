"""Google's change notifications: reading the changes of the calendar that sent one, and
delivering each conflict they raise."""

import functools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from dispatcher import conflicts, db, sentience_auth, settings
from dispatcher.errors import OperatorError
from dispatcher.google_calendar import GoogleCalendar, SyncTokenExpired

# One delta at a time across the app. A conflict can be found from either of its two
# calendars, and two notifications must not both dispatch it. The app is one process.
_one_delta_at_a_time = threading.Lock()
log = logging.getLogger(__name__)


def _deliver_conflicts(
    google: GoogleCalendar, calendar_id: str, events: list[dict]
) -> None:
    """Dispatch each conflict the changed events raise that has not been delivered before.

    Each delivered conflict is recorded at once, so a failure partway leaves the earlier ones
    recorded and the retry dispatches only the rest.
    """
    # The domain is needed only to classify an event that has passed the exclusions.
    events = [event for event in events if conflicts.is_evaluable(event)]
    if not events:
        return
    now = datetime.now(UTC)
    domain = settings.require("GOOGLE_WORKSPACE_DOMAIN")

    # Asked of Google at most once per delta, and only when something needs it.
    @functools.cache
    def time_zone() -> str:
        return _primary_time_zone(google)

    # Cross-Calendar Ingress Timing: the other calendars are read on demand, never stored.
    active_calendars = [channel.calendar_id for channel in db.get_active_channels()]
    for event in events:
        incoming = conflicts.meeting_from_event(calendar_id, event, domain, time_zone)
        # A meeting that has ended can't have an overlap still ahead. A series' own interval
        # is its first occurrence's, so it says nothing about the occurrences still to come.
        if incoming is None or (
            not incoming.recurrence and conflicts.has_ended(incoming.end, now)
        ):
            continue
        found = _conflicts_with_the_calendars(
            google, incoming, active_calendars, now, domain, time_zone
        )
        for conflict in found:
            event_hash = conflicts.event_hash(conflict)
            if db.is_conflict_processed(event_hash):
                continue
            sentience_auth.deliver(conflicts.message(conflict, time_zone()))
            db.record_processed_conflict(event_hash, calendar_id, int(time.time()))


def _conflicts_with_the_calendars(
    google: GoogleCalendar,
    incoming: conflicts.Meeting,
    calendar_ids: list[str],
    now: datetime,
    domain: str,
    time_zone: Callable[[], str],
) -> list[conflicts.Conflict]:
    """The conflicts the incoming meeting raises against the events of the calendars.

    A series is checked against every event that hasn't ended, however far ahead; a single
    event, against what overlaps it (Recurring Series as One Commitment, Look-ahead).
    """
    start, end = (now, None) if incoming.recurrence else (incoming.start, incoming.end)
    listed = {
        calendar_id: google.events_between(calendar_id, start, end)
        for calendar_id in calendar_ids
    }
    # An occurrence edited on its own is listed as an event of its own, and then it is not an
    # occurrence of its series any more.
    edited_on_their_own = {
        calendar_id: {e["id"] for e in events if "recurringEventId" in e}
        for calendar_id, events in listed.items()
    }

    # The series' occurrences are the same for every event it is compared with in one window.
    @functools.cache
    def occurrences(calendar_id: str, series_id: str, window: conflicts.Interval):
        return conflicts.occurrences(
            google.series_occurrences(calendar_id, series_id, *window),
            edited_on_their_own.get(calendar_id, set()),
            time_zone,
        )

    found = []
    for calendar_id, events in listed.items():
        for event in events:
            other = conflicts.meeting_from_event(calendar_id, event, domain, time_zone)
            if other is None or conflicts.is_itself(incoming, other):
                continue
            mine = incoming
            if incoming.recurrence or other.recurrence:
                window = conflicts.look_ahead(incoming, other, now)
                if incoming.recurrence:
                    mine = replace(
                        incoming,
                        occurrences=occurrences(
                            incoming.calendar_id, incoming.event_id, window
                        ),
                    )
                if other.recurrence:
                    other = replace(
                        other,
                        occurrences=occurrences(calendar_id, other.event_id, window),
                    )
            found += conflicts.find_conflicts(mine, [other], now)
    return found


def _primary_time_zone(google: GoogleCalendar) -> str:
    primary = google.get_calendar("primary")
    if primary is None or not primary.get("timeZone"):
        raise OperatorError("Google reported no time zone for the primary calendar")
    return primary["timeZone"]


def process_notification(channel_id: str) -> bool:
    """Read the changes of the calendar that owns the channel, deliver the conflicts they
    raise, and move its cursor forward.

    False, with no Google call, when no active calendar owns the channel. Raises OperatorError
    when the read or a delivery fails, and then the stored cursor is unchanged, except after a
    410.
    """
    with _one_delta_at_a_time:
        calendar = db.get_active_calendar_by_channel(channel_id)
        if calendar is None:
            return False
        calendar_id, sync_token = calendar
        google = GoogleCalendar()
        events = next_sync_token = None
        if sync_token:
            try:
                events, next_sync_token = google.read_events(calendar_id, sync_token)
            except SyncTokenExpired:
                # 410 Gone Sync Invalidation: a cleared cursor makes the next notification
                # do the full read again if this one fails.
                db.save_sync_token(calendar_id, None)
        if next_sync_token is None:
            events, next_sync_token = google.read_events(calendar_id)
        _deliver_conflicts(google, calendar_id, events)
        db.save_sync_token(calendar_id, next_sync_token)
        return True


def process_active_calendars() -> None:
    """Process the delta of every active calendar, one at a time, so the conflicts that went
    undelivered are delivered. A calendar that fails is logged and left for its next
    notification, and the rest still run."""
    for channel in db.get_active_channels():
        try:
            process_notification(channel.channel_id)
        except OperatorError as error:
            # The message names no calendar, because the app's log is public.
            log.warning("Processing a calendar's changes failed: %s", error)
