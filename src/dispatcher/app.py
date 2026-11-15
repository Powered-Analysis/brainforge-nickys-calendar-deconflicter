import hmac
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from dispatcher import attach, db, http, ingress, sentience_auth, settings, watches
from dispatcher.errors import OperatorError
from dispatcher.google_calendar import GoogleCalendar


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # A setting is required only by the operation that uses it, so an app without a
    # database still starts and can answer the renewal endpoint's 500.
    if settings.get("TURSO_DATABASE_URL"):
        db.ensure_schema()
    yield


app = FastAPI(lifespan=lifespan)
log = logging.getLogger(__name__)


class _HideCallbackQuery(logging.Filter):
    """Keeps the authorization code in the callback's query string out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple)
            and len(args) == 5
            and isinstance(args[2], str)
            and args[2].startswith(f"{sentience_auth.CALLBACK_PATH}?")
        ):
            record.args = (*args[:2], sentience_auth.CALLBACK_PATH, *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_HideCallbackQuery())


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    # RFC 7235: the scheme name is case-insensitive.
    if scheme.lower() != "bearer":
        return None
    return token.lstrip(" ")


@app.post("/internal/renew-watches")
def renew_watches(authorization: str | None = Header(default=None)) -> Response:
    # Renewal Ping Authentication: an app without the secret is never open.
    secret = settings.get("INTERNAL_CRON_SECRET")
    if not secret:
        raise HTTPException(
            status_code=500, detail="INTERNAL_CRON_SECRET is not configured"
        )
    token = _bearer_token(authorization)
    if token is None or not hmac.compare_digest(token.encode(), secret.encode()):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # The body names no calendar, channel, or event: the renewal workflow's log is public.
    expired = False
    failures = []
    try:
        # Sentience's standing signal: the renewal still runs, and then fails.
        expired = db.is_sentience_auth_expired()
        # With nothing to renew, no Google or address setting is needed.
        if channels := db.get_active_channels():
            failures = watches.renew_all(
                GoogleCalendar(),
                channels,
                attach.webhook_address(settings.require("PUBLIC_BASE_URL")),
            )
    except OperatorError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except db.Error:
        # The database's own message could carry a calendar ID, so it is left out.
        failures = ["the database failed before any calendar was renewed"]
    for message in failures:
        log.warning("Renewal failed: %s", message)
    if expired:
        return Response(status_code=503)
    return Response(status_code=502 if failures else 200)


@app.post(attach.WEBHOOK_PATH)
def gcal_webhook(x_goog_channel_id: str | None = Header(default=None)) -> Response:
    if not x_goog_channel_id:
        raise HTTPException(status_code=400, detail="X-Goog-Channel-ID is missing")
    try:
        known = ingress.process_notification(x_goog_channel_id)
    except OperatorError as error:
        # A 503 makes Google retry the notification.
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not known:
        raise HTTPException(
            status_code=404, detail="No active calendar has this channel"
        )
    return Response(status_code=200)


def _page(message: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><title>Sentience</title><p>{message}</p>",
        status_code=status_code,
    )


@app.get(sentience_auth.CALLBACK_PATH)
def sentience_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    iss: str | None = None,
) -> HTMLResponse:
    not_connected = "Sentience wasn't connected."
    if error is not None or not code or not state:
        return _page(not_connected, 400)
    if iss is not None and not sentience_auth.issuer_matches(
        iss, settings.require("SENTIENCE_AUTH_ISSUER")
    ):
        return _page(not_connected, 400)
    try:
        with http.client() as client:
            connected = sentience_auth.complete_authorization(
                client, state, code, int(time.time())
            )
    except sentience_auth.TokenRequestFailed:
        return _page(not_connected, 502)
    if not connected:
        return _page(not_connected, 400)
    # Recovery: the conflicts that went undelivered while access was lost.
    ingress.process_active_calendars()
    return _page("Sentience Connected Successfully.", 200)


@app.get(sentience_auth.REAUTH_PATH)
def sentience_reauth() -> Response:
    # The link is public, and whoever finishes the sign-in it starts chooses which Sentience
    # account receives the operator's conflicts, so it works only while access has lapsed.
    try:
        if not db.is_sentience_auth_expired():
            return _page("Sentience doesn't need to be reauthorized.", 409)
        with http.client() as client:
            url = sentience_auth.start_authorization(client, int(time.time()))
    except OperatorError:
        return _page("Sentience couldn't be reached to start the sign-in.", 502)
    return RedirectResponse(url, status_code=302)
