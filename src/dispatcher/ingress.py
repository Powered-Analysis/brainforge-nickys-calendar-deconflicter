"""Google's change notifications: reading the changes of the calendar that sent one, and
delivering each conflict they raise."""

import functools
import threading
import time
from datetime import UTC, datetime

from dispatcher import conflicts, db, sentience_mcp, settings
from dispatcher.errors import OperatorError
from dispatcher.google_calendar import GoogleCalendar, SyncTokenExpired

# One delta at a time across the app. A conflict can be found from either of its two
# calendars, and two notifications must not both dispatch it. The app is one process.
_one_delta_at_a_time = threading.Lock()


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
        # A meeting that has ended can't have an overlap still ahead.
        if incoming is None or incoming.end <= now:
            continue
        others = [
            meeting
            for other_calendar in active_calendars
            for other in google.events_between(
                other_calendar, incoming.start, incoming.end
            )
            if (
                meeting := conflicts.meeting_from_event(
                    other_calendar, other, domain, time_zone
                )
            )
        ]
        for conflict in conflicts.find_conflicts(incoming, others, now):
            event_hash = conflicts.event_hash(conflict)
            if db.is_conflict_processed(event_hash):
                continue
            access_token = db.get_sentience_access_token()
            if access_token is None:
                raise sentience_mcp.DispatchFailed("Sentience is not connected")
            sentience_mcp.dispatch(
                access_token, conflicts.message(conflict, time_zone())
            )
            db.record_processed_conflict(event_hash, calendar_id, int(time.time()))


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
