import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response

from dispatcher import db, settings


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # A setting is required only by the operation that uses it, so an app without a
    # database still starts and can answer the renewal endpoint's 500.
    if settings.get("TURSO_DATABASE_URL"):
        db.ensure_schema()
    yield


app = FastAPI(lifespan=lifespan)


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
