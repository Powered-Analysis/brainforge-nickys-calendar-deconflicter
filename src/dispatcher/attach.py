"""Putting calendars under monitoring: `calendar attach`."""

import uuid
from dataclasses import dataclass
from typing import Literal

from dispatcher import db
from dispatcher.errors import OperatorError
from dispatcher.google_calendar import GoogleCalendar, GoogleRequestFailed

WEBHOOK_PATH = "/webhooks/gcal"
ALL_CALENDARS = "all"


@dataclass(frozen=True)
class Outcome:
    calendar_id: str
    result: Literal["attached", "skipped", "refused", "failed"]
    detail: str = ""


def webhook_address(public_base_url: str) -> str:
    return f"{public_base_url.rstrip('/')}{WEBHOOK_PATH}"


def watch_body(channel_id: str, address: str) -> dict:
    return {"id": channel_id, "type": "web_hook", "address": address}


def is_owned(entry: dict) -> bool:
    return entry.get("accessRole") == "owner"


def title(entry: dict) -> str:
    return entry.get("summaryOverride") or entry.get("summary") or ""


def watch_record(response: dict) -> tuple[str, int]:
    """The resource ID and expiration of a watch response; the expiration is Google's, in ms."""
    resource_id = response.get("resourceId")
    expiration = response.get("expiration")
    if not isinstance(resource_id, str) or not resource_id:
        raise GoogleRequestFailed("Google's watch response had no resourceId")
    try:
        return resource_id, int(expiration)
    except TypeError, ValueError:
        raise GoogleRequestFailed("Google's watch response had no expiration") from None


# The functions up to here decide without touching the network or the database.


def _attach_entry(
    google: GoogleCalendar, entry: dict, address: str, now: int
) -> Outcome:
    """Attach one `CalendarList` entry. Raises OperatorError when a Google call fails."""
    calendar_id = entry["id"]
    if not is_owned(entry):
        return Outcome(calendar_id, "refused", "you do not own it")
    if db.get_active_channel(calendar_id) is not None:
        return Outcome(calendar_id, "skipped", "already attached")
    sync_token = google.read_sync_token(calendar_id)
    channel_id = str(uuid.uuid4())
    resource_id, expiration = watch_record(
        google.watch(calendar_id, watch_body(channel_id, address))
    )
    try:
        db.save_attached_calendar(
            calendar_id, channel_id, resource_id, expiration, sync_token, now
        )
    except Exception:
        # Without a row, renewal and detach could never find the channel, and Google would keep
        # sending notifications for it until it expires.
        google.stop_channel(channel_id, resource_id)
        raise
    return Outcome(calendar_id, "attached", title(entry))


def attach_one(
    google: GoogleCalendar, calendar_ref: str, address: str, now: int
) -> Outcome:
    entry = google.get_calendar(calendar_ref)
    if entry is None:
        return Outcome(calendar_ref, "refused", "it is not on your calendar list")
    return _attach_entry(google, entry, address, now)


def attach_all(google: GoogleCalendar, address: str, now: int) -> list[Outcome]:
    """Attach every owned calendar that is not active; a failure on one does not stop the rest."""
    outcomes = []
    for entry in google.list_calendars():
        if not is_owned(entry):
            continue
        try:
            outcomes.append(_attach_entry(google, entry, address, now))
        except OperatorError as error:
            outcomes.append(Outcome(entry["id"], "failed", str(error)))
    return outcomes
