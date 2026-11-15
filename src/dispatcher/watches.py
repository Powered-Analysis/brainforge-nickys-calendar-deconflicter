"""Keeping a calendar's Google subscription alive, and ending it: renewal and `calendar detach`."""

import uuid

from dispatcher import attach, db
from dispatcher.errors import OperatorError
from dispatcher.google_calendar import GoogleCalendar, GoogleRequestFailed


def _renew(google: GoogleCalendar, channel: db.Channel, address: str) -> None:
    # The new channel is recorded before the old one is stopped, so a failed stop never loses
    # the channel that is now live.
    new_channel_id = str(uuid.uuid4())
    resource_id, expiration = attach.watch_record(
        google.watch(channel.calendar_id, attach.watch_body(new_channel_id, address))
    )
    try:
        db.replace_channel(channel.calendar_id, new_channel_id, resource_id, expiration)
    except Exception as save_error:
        attach.stop_unrecorded_channel(google, new_channel_id, resource_id, save_error)
        raise
    google.stop_channel(channel.channel_id, channel.resource_id)


def renew_all(
    google: GoogleCalendar, channels: list[db.Channel], address: str
) -> list[str]:
    """Renew each channel and return one message per calendar that failed.

    A failure on one calendar does not stop the rest. The messages name no calendar, because
    the renewal workflow's log is public.
    """
    failures = []
    for channel in channels:
        try:
            _renew(google, channel, address)
        except OperatorError as error:
            failures.append(str(error))
    return failures


def detach(google: GoogleCalendar, calendar_id: str) -> None:
    """Close the calendar's channel and mark it inactive.

    Raises OperatorError, leaving the calendar active, when it is not attached or when Google
    does not close the channel.
    """
    channel = db.get_active_channel(calendar_id)
    if channel is None:
        raise OperatorError(f"{calendar_id} is not attached")
    try:
        google.stop_channel(channel.channel_id, channel.resource_id)
    except GoogleRequestFailed as error:
        raise OperatorError(
            f"detaching {calendar_id} failed, and it is still attached: {error}"
        ) from error
    db.deactivate_calendar(calendar_id)
