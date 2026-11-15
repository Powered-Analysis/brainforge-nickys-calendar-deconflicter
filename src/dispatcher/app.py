import hmac
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse

from dispatcher import db, http, sentience_auth, settings


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # A setting is required only by the operation that uses it, so an app without a
    # database still starts and can answer the renewal endpoint's 500.
    if settings.get("TURSO_DATABASE_URL"):
        db.ensure_schema()
    yield


app = FastAPI(lifespan=lifespan)


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


@app.post("/internal/renew-watches", status_code=204)
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
    return Response(status_code=204)


def _page(message: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><title>Sentience</title><p>{message}</p>",
        status_code=status_code,
    )


@app.get(sentience_auth.CALLBACK_PATH)
def sentience_callback(
    code: str | None = None, state: str | None = None, error: str | None = None
) -> HTMLResponse:
    not_connected = "Sentience wasn't connected."
    if error is not None or not code or not state:
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
    return _page("Sentience Connected Successfully.", 200)
