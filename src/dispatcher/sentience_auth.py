"""Connecting the dispatcher to Sentience: client registration and the PKCE authorization."""

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx2

from dispatcher import alert, db, http, sentience_mcp, settings
from dispatcher.errors import OperatorError

CALLBACK_PATH = "/oauth/sentience/callback"
REAUTH_PATH = "/oauth/sentience/reauth"
# Token Rotation & Refresh Management: a token is renewed this long before it lapses.
REFRESH_MARGIN_SECONDS = 60
# How long an operator has to finish signing in before the pending authorization is discarded.
PENDING_AUTHORIZATION_LIFETIME_SECONDS = 600
CLIENT_NAME = "Calendar Deconflicter"
SCOPE = "sentience"

log = logging.getLogger(__name__)


class TokenRequestFailed(OperatorError):
    """Sentience's token endpoint did not return usable tokens."""


class NotConnected(OperatorError):
    """No Sentience connection is stored, so the operator must connect."""


class AuthExpired(OperatorError):
    """Sentience has refused the refresh token, so the operator must reconnect."""


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: int


# The functions up to `_json_object` decide without touching the network or the database.


def new_code_verifier() -> str:
    # RFC 7636 section 4.1: 86 characters of the unreserved set, from a secure random source.
    return secrets.token_urlsafe(64)


def code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def endpoint(issuer: str, name: str) -> str:
    return f"{issuer.rstrip('/')}/oauth/mcp-issuer/{name}"


def issuer_matches(iss: str, issuer: str) -> bool:
    # RFC 9207: a response that names its issuer must name the one this app configured.
    return iss == issuer.rstrip("/")


def callback_url(public_base_url: str) -> str:
    return f"{public_base_url.rstrip('/')}{CALLBACK_PATH}"


def reauth_url(public_base_url: str) -> str:
    return f"{public_base_url.rstrip('/')}{REAUTH_PATH}"


def needs_refresh(expires_at: int, now: int) -> bool:
    return expires_at <= now + REFRESH_MARGIN_SECONDS


def pending_authorization_cutoff(now: int) -> int:
    """Pending authorizations created before this moment have expired."""
    return now - PENDING_AUTHORIZATION_LIFETIME_SECONDS


def refresh_request_form(client_id: str, refresh_token: str) -> dict:
    return {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }


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


def _json_object(response: httpx2.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def is_invalid_grant(response: httpx2.Response) -> bool:
    # OAuth sends `invalid_grant` with 400 and the PRD names 401: the error decides, not the status.
    return (
        not response.is_success
        and _json_object(response).get("error") == "invalid_grant"
    )


def _failure_reason(response: httpx2.Response) -> str:
    """The status and the OAuth error fields, which never hold a credential."""
    body = _json_object(response)
    detail = " ".join(
        value
        for value in (body.get("error"), body.get("error_description"))
        if isinstance(value, str)
    )
    return f"HTTP {response.status_code} {detail}".strip()


def _post_token_form(client: httpx2.Client, form: dict) -> httpx2.Response:
    issuer = settings.require("SENTIENCE_AUTH_ISSUER")
    try:
        return client.post(endpoint(issuer, "token"), data=form)
    except httpx2.HTTPError as exc:
        log.warning("Sentience token request failed: %s", type(exc).__name__)
        raise TokenRequestFailed(type(exc).__name__) from exc


def _tokens_from_response(response: httpx2.Response, now: int) -> Tokens:
    tokens = (
        tokens_from_response(_json_object(response), now)
        if response.is_success
        else None
    )
    if tokens is None:
        reason = _failure_reason(response)
        log.warning("Sentience token request failed: %s", reason)
        raise TokenRequestFailed(reason)
    return tokens


def register_client(client: httpx2.Client, issuer: str, redirect_uri: str) -> str:
    try:
        response = client.post(
            endpoint(issuer, "register"), json=registration_body(redirect_uri)
        )
    except httpx2.HTTPError as exc:
        raise OperatorError(
            f"Sentience registration failed: {type(exc).__name__}: {exc}"
        ) from exc
    client_id = _json_object(response).get("client_id")
    if not response.is_success or not isinstance(client_id, str) or not client_id:
        raise OperatorError(
            f"Sentience registration failed: {_failure_reason(response)}"
        )
    return client_id


def start_authorization(client: httpx2.Client, now: int) -> str:
    """Register if no client is stored, store a pending authorization, and return the sign-in URL."""
    issuer = settings.require("SENTIENCE_AUTH_ISSUER")
    redirect_uri = callback_url(settings.require("PUBLIC_BASE_URL"))
    client_id = db.get_sentience_client_id()
    if client_id is None:
        client_id = register_client(client, issuer, redirect_uri)
        db.save_sentience_client_id(client_id)
    # Abandoned runs would otherwise keep a usable verifier and state forever.
    db.delete_expired_pending_authorizations(pending_authorization_cutoff(now))
    code_verifier = new_code_verifier()
    state = secrets.token_urlsafe(32)
    db.save_pending_authorization(state, code_verifier, now)
    return authorize_url(issuer, client_id, redirect_uri, code_verifier, state)


def complete_authorization(
    client: httpx2.Client, state: str, code: str, now: int
) -> bool:
    """Exchange the code for tokens and store them.

    False, with no token request made, when the state is not a pending authorization or its
    lifetime has passed.
    Raises TokenRequestFailed, with nothing stored, when Sentience does not return tokens.
    """
    # Deleting first makes an expired state indistinguishable from an unknown one.
    db.delete_expired_pending_authorizations(pending_authorization_cutoff(now))
    code_verifier = db.get_pending_code_verifier(state)
    client_id = db.get_sentience_client_id()
    if code_verifier is None or client_id is None:
        return False
    redirect_uri = callback_url(settings.require("PUBLIC_BASE_URL"))
    tokens = _tokens_from_response(
        _post_token_form(
            client,
            token_request_form(client_id, redirect_uri, code, code_verifier),
        ),
        now,
    )
    return db.store_sentience_tokens(
        state,
        client_id,
        tokens.access_token,
        tokens.refresh_token,
        tokens.expires_at,
        now,
    )


def _expire(client: httpx2.Client, now: int) -> None:
    """Mark access expired and send the alert once for this expiry."""
    if db.mark_sentience_auth_expired(now):
        alert.send_reauthorization_alert(
            client, reauth_url(settings.require("PUBLIC_BASE_URL"))
        )


def _stored_auth() -> db.StoredAuth:
    auth = db.get_sentience_auth()
    if auth is None:
        raise NotConnected("Sentience is not connected")
    return auth


def _refresh(now: int) -> str:
    """Renew the stored tokens and return the new access token.

    Raises AuthExpired, after marking access expired, when Sentience refuses the refresh token,
    NotConnected when nothing is stored, and TokenRequestFailed, with nothing changed, for any
    other failure.
    """
    auth = _stored_auth()
    if auth.refresh_token is None:
        raise TokenRequestFailed("Sentience gave no refresh token")
    with http.client() as client:
        response = _post_token_form(
            client, refresh_request_form(auth.client_id, auth.refresh_token)
        )
        if is_invalid_grant(response):
            log.warning("Sentience refused the refresh token")
            _expire(client, now)
            raise AuthExpired("Sentience refused the refresh token")
    tokens = _tokens_from_response(response, now)
    db.save_refreshed_tokens(
        tokens.access_token, tokens.refresh_token, tokens.expires_at, now
    )
    return tokens.access_token


def deliver(message: str) -> None:
    """Dispatch one conflict message with a fresh access token, or raise OperatorError.

    The token is renewed first when it has lapsed or is about to, and once more, with one
    retry of the dispatch, when Sentience rejects it. While access is expired nothing is
    refreshed and Sentience is not called.
    """
    if db.is_sentience_auth_expired():
        raise AuthExpired("Sentience access has expired and must be reauthorized")
    auth = _stored_auth()
    now = int(time.time())
    access_token = auth.access_token
    if needs_refresh(auth.expires_at, now):
        access_token = _refresh(now)
    try:
        sentience_mcp.dispatch(access_token, message)
    except sentience_mcp.AccessTokenRejected:
        sentience_mcp.dispatch(_refresh(now), message)
