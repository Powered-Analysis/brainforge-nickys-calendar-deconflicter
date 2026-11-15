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
        except attach.ChannelLeftOpen as error:
            # The save error's text is left out for the same reason.
            failures.append(
                f"saving a renewed channel failed, and {error.stop_summary()}"
            )
        except OperatorError as error:
            failures.append(str(error))
        except db.Error:
            # The database's own message could carry a calendar ID, so it is left out.
            failures.append("the database failed while renewing a calendar")
    return failures


def detach(google: GoogleCalendar, calendar_id: str) -> None:
    """Close the calendar's channel and mark it inactive.

    Raises OperatorError, leaving the calendar active, when it is not attached, when Google
    does not close the channel, or when the database fails. A detach that failed on the
    database after Google closed the channel can be run again: Google's answer for a channel
    that is already closed counts as closed.
    """
    try:
        _detach(google, calendar_id)
    except db.Error as error:
        raise OperatorError(db.failure_message(error)) from error


def _detach(google: GoogleCalendar, calendar_id: str) -> None:
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
