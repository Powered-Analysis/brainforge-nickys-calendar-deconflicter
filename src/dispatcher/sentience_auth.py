"""Connecting the dispatcher to Sentience: client registration and the PKCE authorization."""

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from dispatcher import db, settings
from dispatcher.errors import OperatorError

CALLBACK_PATH = "/oauth/sentience/callback"
CLIENT_NAME = "Calendar Deconflicter"
SCOPE = "sentience"

log = logging.getLogger(__name__)


class TokenRequestFailed(OperatorError):
    """Sentience's token endpoint did not return usable tokens."""


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: int


# The functions up to `tokens_from_response` decide without touching the network or the database.


def new_code_verifier() -> str:
    # RFC 7636 section 4.1: 86 characters of the unreserved set, from a secure random source.
    return secrets.token_urlsafe(64)


def code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def endpoint(issuer: str, name: str) -> str:
    return f"{issuer.rstrip('/')}/oauth/mcp-issuer/{name}"


def callback_url(public_base_url: str) -> str:
    return f"{public_base_url.rstrip('/')}{CALLBACK_PATH}"


def registration_body(redirect_uri: str) -> dict:
    return {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def authorize_url(
    issuer: str, client_id: str, redirect_uri: str, code_verifier: str, state: str
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "code_challenge": code_challenge(code_verifier),
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{endpoint(issuer, 'authorize')}?{query}"


def token_request_form(
    client_id: str, redirect_uri: str, code: str, code_verifier: str
) -> dict:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }


def tokens_from_response(payload: dict, now: int) -> Tokens | None:
    """None when the response carries no access token."""
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    # Without a lifetime the token counts as already expired, so it is refreshed before use.
    lifetime = (
        expires_in
        if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
        else 0
    )
    return Tokens(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        expires_at=now + int(lifetime),
    )


def _json_object(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _failure_reason(response: httpx.Response) -> str:
    """The status and the OAuth error fields, which never hold a credential."""
    body = _json_object(response)
    detail = " ".join(
        value
        for value in (body.get("error"), body.get("error_description"))
        if isinstance(value, str)
    )
    return f"HTTP {response.status_code} {detail}".strip()


def register_client(http: httpx.Client, issuer: str, redirect_uri: str) -> str:
    try:
        response = http.post(
            endpoint(issuer, "register"), json=registration_body(redirect_uri)
        )
    except httpx.HTTPError as exc:
        raise OperatorError(
            f"Sentience registration failed: {type(exc).__name__}: {exc}"
        ) from exc
    client_id = _json_object(response).get("client_id")
    if not response.is_success or not isinstance(client_id, str) or not client_id:
        raise OperatorError(
            f"Sentience registration failed: {_failure_reason(response)}"
        )
    return client_id


def start_authorization(http: httpx.Client, now: int) -> str:
    """Register if no client is stored, store a pending authorization, and return the sign-in URL."""
    issuer = settings.require("SENTIENCE_AUTH_ISSUER")
    redirect_uri = callback_url(settings.require("PUBLIC_BASE_URL"))
    client_id = db.get_sentience_client_id()
    if client_id is None:
        client_id = register_client(http, issuer, redirect_uri)
        db.save_sentience_client_id(client_id)
    code_verifier = new_code_verifier()
    state = secrets.token_urlsafe(32)
    db.save_pending_authorization(state, code_verifier, now)
    return authorize_url(issuer, client_id, redirect_uri, code_verifier, state)


def complete_authorization(http: httpx.Client, state: str, code: str, now: int) -> bool:
    """Exchange the code for tokens and store them.

    False, with no token request made, when the state is not a pending authorization.
    Raises TokenRequestFailed, with nothing stored, when Sentience does not return tokens.
    """
    code_verifier = db.get_pending_code_verifier(state)
    client_id = db.get_sentience_client_id()
    if code_verifier is None or client_id is None:
        return False
    issuer = settings.require("SENTIENCE_AUTH_ISSUER")
    redirect_uri = callback_url(settings.require("PUBLIC_BASE_URL"))
    try:
        response = http.post(
            endpoint(issuer, "token"),
            data=token_request_form(client_id, redirect_uri, code, code_verifier),
        )
    except httpx.HTTPError as exc:
        log.warning("Sentience token request failed: %s", type(exc).__name__)
        raise TokenRequestFailed(type(exc).__name__) from exc
    tokens = (
        tokens_from_response(_json_object(response), now)
        if response.is_success
        else None
    )
    if tokens is None:
        reason = _failure_reason(response)
        log.warning("Sentience token request failed: %s", reason)
        raise TokenRequestFailed(reason)
    return db.store_sentience_tokens(
        state,
        client_id,
        tokens.access_token,
        tokens.refresh_token,
        tokens.expires_at,
        now,
    )
