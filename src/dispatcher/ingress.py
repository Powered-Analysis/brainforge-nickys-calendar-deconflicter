"""Google's change notifications: reading the changes of the calendar that sent one."""

import threading

from dispatcher import db
from dispatcher.google_calendar import GoogleCalendar, SyncTokenExpired

# One delta at a time across the app. A conflict can be found from either of its two
# calendars, and two notifications must not both dispatch it. The app is one process.
_one_delta_at_a_time = threading.Lock()


def process_notification(channel_id: str) -> bool:
    """Read the changes of the calendar that owns the channel, and move its cursor forward.

    False, with no Google call, when no active calendar owns the channel. Raises OperatorError
    when the read fails, and then the stored cursor is unchanged, except after a 410.
    """
    with _one_delta_at_a_time:
        calendar = db.get_active_calendar_by_channel(channel_id)
        if calendar is None:
            return False
        calendar_id, sync_token = calendar
        google = GoogleCalendar()
        next_sync_token = None
        if sync_token:
            try:
                _, next_sync_token = google.read_events(calendar_id, sync_token)
            except SyncTokenExpired:
                # 410 Gone Sync Invalidation: a cleared cursor makes the next notification
                # do the full read again if this one fails.
                db.save_sync_token(calendar_id, None)
        if next_sync_token is None:
            _, next_sync_token = google.read_events(calendar_id)
        db.save_sync_token(calendar_id, next_sync_token)
        return True
