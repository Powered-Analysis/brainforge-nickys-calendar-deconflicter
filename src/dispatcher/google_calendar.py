"""The one module that calls Google. Every Calendar API request the app makes goes through it."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import google_auth_httplib2
import httplib2
from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from dispatcher import settings
from dispatcher.errors import OperatorError

# Read-only access to calendars is enough for every call the app makes, `events.watch` and
# `channels.stop` included (Calendar v3 reference, each method's authorization scopes).
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"
TIMEOUT_SECONDS = 15.0
PAGE_SIZE = 250

# Tests set this to an `httplib2.Http`-compatible object to replace the network; the app
# leaves it unset. It also serves the token refresh, so a test can answer that request too.
transport: Any = None


class GoogleRequestFailed(OperatorError):
    """Google, or the connection to it, did not complete a request."""


class SyncTokenExpired(GoogleRequestFailed):
    """Google answered 410: it no longer honors the calendar's `syncToken`."""


@contextmanager
def _execute_errors() -> Iterator[None]:
    try:
        yield
    except HttpError as exc:
        raise GoogleRequestFailed(
            f"Google answered HTTP {exc.resp.status}: {exc.reason}"
        ) from exc
    except (GoogleAuthError, httplib2.HttpLib2Error, OSError) as exc:
        raise GoogleRequestFailed(
            f"Google request failed: {type(exc).__name__}: {exc}"
        ) from exc


def _window(start: datetime, end: datetime | None) -> dict[str, str]:
    window = {"timeMin": start.astimezone(UTC).isoformat()}
    if end:
        window["timeMax"] = end.astimezone(UTC).isoformat()
    return window


class GoogleCalendar:
    """The operator's Calendar API session. Reads its credentials from the settings."""

    def __init__(self) -> None:
        credentials = Credentials(
            token=None,
            refresh_token=settings.require("GCP_REFRESH_TOKEN"),
            client_id=settings.require("GCP_CLIENT_ID"),
            client_secret=settings.require("GCP_CLIENT_SECRET"),
            token_uri=TOKEN_URI,
            scopes=[SCOPE],
        )
        http = google_auth_httplib2.AuthorizedHttp(
            credentials, http=transport or httplib2.Http(timeout=TIMEOUT_SECONDS)
        )
        self._api = build("calendar", "v3", http=http)

    def get_calendar(self, calendar_id: str) -> dict | None:
        """The operator's `CalendarList` entry, or None when the calendar is not in it."""
        with _execute_errors():
            try:
                return self._api.calendarList().get(calendarId=calendar_id).execute()
            except HttpError as exc:
                if exc.resp.status == 404:
                    return None
                raise

    def list_calendars(self) -> list[dict]:
        entries: list[dict] = []
        page_token = None
        while True:
            with _execute_errors():
                page = (
                    self._api.calendarList()
                    .list(maxResults=PAGE_SIZE, pageToken=page_token)
                    .execute()
                )
            entries.extend(page.get("items", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return entries

    def _list_events(
        self, calendar_id: str, *, instances_of: str | None = None, **params: Any
    ) -> tuple[list[dict], dict]:
        """Every page of `events.list` (or, for a series id in `instances_of`, of
        `events.instances`) for the parameters, and the last page."""
        events: list[dict] = []
        page_token = None
        while True:
            with _execute_errors():
                try:
                    if instances_of:
                        request = self._api.events().instances(
                            calendarId=calendar_id,
                            eventId=instances_of,
                            maxResults=PAGE_SIZE,
                            pageToken=page_token,
                            **params,
                        )
                    else:
                        request = self._api.events().list(
                            calendarId=calendar_id,
                            maxResults=PAGE_SIZE,
                            pageToken=page_token,
                            **params,
                        )
                    page = request.execute()
                except HttpError as exc:
                    if params.get("syncToken") and exc.resp.status == 410:
                        raise SyncTokenExpired(
                            "Google no longer honors the calendar's syncToken"
                        ) from exc
                    raise
            events.extend(page.get("items", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return events, page

    def read_events(
        self, calendar_id: str, sync_token: str | None = None
    ) -> tuple[list[dict], str]:
        """Every page of `events.list`, and the `syncToken` Google issues for this moment.

        With a `sync_token` the events are only what changed since it; without one, every
        event. Raises SyncTokenExpired when Google answers 410 to a `sync_token`.

        Recurring Series as One Commitment, Reading: `singleEvents` stays unset, so a series
        is one event.
        """
        events, last_page = self._list_events(calendar_id, syncToken=sync_token)
        next_sync_token = last_page.get("nextSyncToken")
        if not next_sync_token:
            raise GoogleRequestFailed("Google returned no syncToken for the calendar")
        return events, next_sync_token

    def events_between(
        self, calendar_id: str, start: datetime, end: datetime | None = None
    ) -> list[dict]:
        """The events that overlap `[start, end)`, or that end after `start` when there is no
        `end`; a series comes back as one event, and so does an occurrence edited on its own.

        Google leaves out an event that ends at or before `start` or starts at or after `end`.
        """
        events, _ = self._list_events(calendar_id, **_window(start, end))
        return events

    def series_occurrences(
        self, calendar_id: str, series_id: str, start: datetime, end: datetime
    ) -> list[dict]:
        """The occurrences of a series that overlap `[start, end)`, from `events.instances`.

        Occurrences edited on their own are among them, and cancelled ones are not.
        """
        events, _ = self._list_events(
            calendar_id, instances_of=series_id, **_window(start, end)
        )
        return events

    def read_sync_token(self, calendar_id: str) -> str:
        """The `syncToken` that marks this moment, from a read of every event."""
        return self.read_events(calendar_id)[1]

    def watch(self, calendar_id: str, body: dict) -> dict:
        """Open a notification channel. This is one of the app's two writes to Google."""
        with _execute_errors():
            return self._api.events().watch(calendarId=calendar_id, body=body).execute()

    def stop_channel(self, channel_id: str, resource_id: str) -> None:
        """Close a notification channel. This is the app's other write to Google.

        A 404 means the channel is already gone, which is what the caller wants.
        """
        with _execute_errors():
            try:
                self._api.channels().stop(
                    body={"id": channel_id, "resourceId": resource_id}
                ).execute()
            except HttpError as exc:
                if exc.resp.status != 404:
                    raise
