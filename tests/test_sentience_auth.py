"""Issue #4: `auth sentience` and `GET /oauth/sentience/callback`.

Sentience's auth server is replaced by an injected `httpx` transport, and the operator's
browser by the one URL-opening function. The database is the libSQL container.
"""

import base64
import hashlib
import html
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient

from dispatcher import cli, db, http, sentience_auth
from dispatcher.app import app

ISSUER = "https://auth.example.com"
BASE_URL = "https://dispatcher-example.fly.dev"
CALLBACK = f"{BASE_URL}/oauth/sentience/callback"
CLIENT_ID = "client-example-123"
ACCESS_TOKEN = "access-token-example-aaa"
REFRESH_TOKEN = "refresh-token-example-bbb"
CODE = "authorization-code-example-ccc"

OLD_TOKENS = ("old-client", "old-access-token", "old-refresh-token", 12345, 6789)

UNRESERVED = re.compile(r"[A-Za-z0-9\-._~]+")


class FakeSentience:
    """Stands in for Sentience's auth server and records every request it receives."""

    def __init__(self):
        self.requests = []
        self.register_response = lambda request: httpx2.Response(
            201, json={"client_id": CLIENT_ID}
        )
        self.token_response = lambda request: httpx2.Response(
            200,
            json={
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/oauth/mcp-issuer/register":
            return self.register_response(request)
        if request.url.path == "/oauth/mcp-issuer/token":
            return self.token_response(request)
        return httpx2.Response(404)

    def to(self, path):
        return [r for r in self.requests if r.url.path == path]

    @property
    def registrations(self):
        return self.to("/oauth/mcp-issuer/register")

    @property
    def token_requests(self):
        return self.to("/oauth/mcp-issuer/token")


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def failing(request):
    raise httpx2.ConnectError("unreachable", request=request)


def reject(request):
    return httpx2.Response(400, json={"error": "invalid_redirect_uri"})


def server_error(request):
    return httpx2.Response(500, text="boom")


def no_client_id(request):
    return httpx2.Response(201, json={})


def invalid_grant(request):
    return httpx2.Response(400, json={"error": "invalid_grant"})


def unauthorized(request):
    return httpx2.Response(401, json={"error": "invalid_client"})


@pytest.fixture(autouse=True)
def database():
    db.ensure_schema()
    wipe()
    yield
    wipe()


def wipe():
    conn = db.connect()
    try:
        for table in (
            "sentience_auth",
            "sentience_client",
            "sentience_pending_authorization",
        ):
            conn.execute(f"DELETE FROM {table}")
    finally:
        conn.close()


def query(sql, params=()):
    conn = db.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def stored_tokens():
    rows = query(
        "SELECT client_id, access_token, refresh_token, expires_at, updated_at"
        " FROM sentience_auth"
    )
    return [tuple(r) for r in rows]


def seed_old_tokens():
    query(
        "INSERT INTO sentience_auth (id, client_id, access_token, refresh_token,"
        " expires_at, updated_at) VALUES ('sentience', ?, ?, ?, ?, ?)",
        OLD_TOKENS,
    )


@pytest.fixture
def fake(monkeypatch):
    server = FakeSentience()
    monkeypatch.setattr(http, "transport", httpx2.MockTransport(server))
    return server


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("SENTIENCE_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)


@pytest.fixture
def browser(monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "open_url", opened.append)
    return opened


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sign_in(fake, settings_env, browser):
    """Runs `auth sentience` and returns the sign-in URL's query parameters."""

    def run():
        before = len(browser)
        cli.main(["auth", "sentience"])
        assert len(browser) == before + 1
        return {k: v[0] for k, v in parse_qs(urlsplit(browser[-1]).query).items()}

    return run


def logged(caplog):
    """The app's log output; the test client's own request log is not the app's."""
    return "\n".join(
        r.getMessage() for r in caplog.records if not r.name.startswith("httpx2")
    )


def callback(client, **params):
    return client.get("/oauth/sentience/callback", params=params)


# --- 1. PKCE verifier and challenge (RFC 7636) ---------------------------------------------


def test_challenge_matches_the_rfc_7636_appendix_b_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert (
        sentience_auth.code_challenge(verifier)
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


@pytest.mark.parametrize("verifier", ["a" * 43, "~" * 128, "x-._~Z9" * 10])
def test_challenge_is_unpadded_base64url_of_sha256(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    challenge = sentience_auth.code_challenge(verifier)
    assert challenge == expected
    assert "=" not in challenge


def test_verifiers_are_in_range_unreserved_and_not_repeated():
    verifiers = [sentience_auth.new_code_verifier() for _ in range(50)]
    for verifier in verifiers:
        assert 43 <= len(verifier) <= 128
        assert UNRESERVED.fullmatch(verifier)
    assert len(set(verifiers)) == len(verifiers)


# --- 2. Registration ------------------------------------------------------------------------


def test_first_run_registers_with_the_specified_body_and_stores_the_client(
    fake, settings_env, browser
):
    cli.main(["auth", "sentience"])
    assert len(fake.registrations) == 1
    request = fake.registrations[0]
    assert request.method == "POST"
    assert str(request.url) == f"{ISSUER}/oauth/mcp-issuer/register"
    body = json.loads(request.content)
    assert isinstance(body["client_name"], str) and body["client_name"]
    assert body["redirect_uris"] == [CALLBACK]
    assert sorted(body["grant_types"]) == ["authorization_code", "refresh_token"]
    assert body["response_types"] == ["code"]
    assert body["token_endpoint_auth_method"] == "none"
    assert [tuple(r) for r in query("SELECT client_id FROM sentience_client")] == [
        (CLIENT_ID,)
    ]


def test_second_run_reuses_the_stored_client(sign_in, fake):
    first = sign_in()
    second = sign_in()
    assert len(fake.registrations) == 1
    assert first["client_id"] == second["client_id"] == CLIENT_ID


def test_a_client_stored_beforehand_is_used_without_registering(sign_in, fake):
    db.save_sentience_client_id("preexisting-client")
    assert sign_in()["client_id"] == "preexisting-client"
    assert fake.registrations == []


def test_issuer_with_trailing_slash_gives_the_same_endpoints(
    fake, monkeypatch, browser
):
    monkeypatch.setenv("SENTIENCE_AUTH_ISSUER", ISSUER + "/")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    cli.main(["auth", "sentience"])
    assert str(fake.registrations[0].url) == f"{ISSUER}/oauth/mcp-issuer/register"
    assert browser[0].startswith(f"{ISSUER}/oauth/mcp-issuer/authorize?")


# --- 3. Pending authorization and the sign-in URL -------------------------------------------


def test_browser_opens_at_the_authorize_endpoint_with_the_specified_parameters(
    fake, settings_env, browser
):
    cli.main(["auth", "sentience"])
    assert len(browser) == 1
    parts = urlsplit(browser[0])
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        f"{ISSUER}/oauth/mcp-issuer/authorize"
    )
    raw = parse_qs(parts.query, keep_blank_values=True)
    assert all(len(v) == 1 for v in raw.values())
    params = {k: v[0] for k, v in raw.items()}
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == CALLBACK
    assert params["scope"] == "sentience"
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"] and params["state"]


def test_the_sign_in_url_is_also_printed(fake, settings_env, browser, capsys):
    cli.main(["auth", "sentience"])
    assert browser[0] in capsys.readouterr().out


def test_every_run_has_its_own_state_and_challenge(sign_in):
    first, second = sign_in(), sign_in()
    assert first["state"] != second["state"]
    assert first["code_challenge"] != second["code_challenge"]


def test_the_verifier_behind_the_challenge_reaches_the_token_request(
    sign_in, fake, client
):
    params = sign_in()
    callback(client, code=CODE, state=params["state"])
    verifier = form(fake.token_requests[0])["code_verifier"]
    assert 43 <= len(verifier) <= 128
    assert UNRESERVED.fullmatch(verifier)
    assert sentience_auth.code_challenge(verifier) == params["code_challenge"]


# --- 4. Registration failure ----------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [reject, server_error, failing, no_client_id],
    ids=["rejected", "server-error", "transport-failure", "no-client-id"],
)
def test_registration_failure_exits_nonzero_and_opens_no_browser(
    fake, settings_env, browser, capsys, handler
):
    fake.register_response = handler
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["auth", "sentience"])
    assert exit_info.value.code not in (0, None)
    assert browser == []
    seen = capsys.readouterr()
    assert seen.err.strip()
    assert "Traceback" not in seen.err
    assert query("SELECT client_id FROM sentience_client") == []
    assert query("SELECT state FROM sentience_pending_authorization") == []


@pytest.mark.parametrize("missing", ["SENTIENCE_AUTH_ISSUER", "PUBLIC_BASE_URL"])
def test_a_missing_setting_exits_nonzero_before_any_request(
    fake, settings_env, browser, monkeypatch, missing
):
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["auth", "sentience"])
    assert exit_info.value.code not in (0, None)
    assert browser == []
    assert fake.requests == []


# --- 5. Callback success --------------------------------------------------------------------


def test_callback_posts_the_token_form_stores_tokens_and_confirms(
    sign_in, fake, client
):
    params = sign_in()
    before = int(time.time())
    response = callback(client, code=CODE, state=params["state"])
    after = int(time.time())

    assert response.status_code == 200
    assert "Sentience Connected Successfully." in html.unescape(response.text)

    assert len(fake.token_requests) == 1
    request = fake.token_requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{ISSUER}/oauth/mcp-issuer/token"
    assert request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    sent = form(request)
    assert sent.pop("code_verifier")
    assert sent == {
        "grant_type": "authorization_code",
        "code": CODE,
        "redirect_uri": CALLBACK,
        "client_id": CLIENT_ID,
    }

    [(client_id, access, refresh, expires_at, updated_at)] = stored_tokens()
    assert (client_id, access, refresh) == (CLIENT_ID, ACCESS_TOKEN, REFRESH_TOKEN)
    assert before + 3600 <= expires_at <= after + 3600
    assert before <= updated_at <= after


def test_a_response_without_expires_in_is_already_expired(sign_in, fake, client):
    fake.token_response = lambda request: httpx2.Response(
        200, json={"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN}
    )
    params = sign_in()
    before = int(time.time())
    callback(client, code=CODE, state=params["state"])
    after = int(time.time())
    [(_, _, _, expires_at, _)] = stored_tokens()
    assert before <= expires_at <= after


def test_a_response_without_a_refresh_token_stores_none(sign_in, fake, client):
    fake.token_response = lambda request: httpx2.Response(
        200, json={"access_token": ACCESS_TOKEN, "expires_in": 60}
    )
    params = sign_in()
    response = callback(client, code=CODE, state=params["state"])
    assert response.status_code == 200
    [(_, access, refresh, _, _)] = stored_tokens()
    assert access == ACCESS_TOKEN
    assert refresh is None


def test_a_new_authorization_replaces_the_old_tokens_in_the_single_row(
    sign_in, fake, client
):
    seed_old_tokens()
    params = sign_in()
    callback(client, code=CODE, state=params["state"])
    rows = stored_tokens()
    assert len(rows) == 1
    assert rows[0][:3] == (CLIENT_ID, ACCESS_TOKEN, REFRESH_TOKEN)


# --- 6. Callback rejections -----------------------------------------------------------------


def assert_rejected_without_a_token_request(fake, response):
    assert response.status_code == 400
    assert fake.token_requests == []
    assert stored_tokens() == [OLD_TOKENS]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"code": CODE},
        {"state": "anything"},
        {"code": "", "state": ""},
        {"code": CODE, "state": "never-issued"},
    ],
    ids=["nothing", "no-state", "no-code", "empty", "unknown-state"],
)
def test_missing_or_unknown_state_is_a_400(fake, settings_env, client, params):
    seed_old_tokens()
    assert_rejected_without_a_token_request(fake, callback(client, **params))


def test_a_used_state_is_a_400_and_stores_nothing_new(sign_in, fake, client):
    params = sign_in()
    assert callback(client, code=CODE, state=params["state"]).status_code == 200
    fake.token_response = lambda request: httpx2.Response(
        200, json={"access_token": "replayed-access", "expires_in": 60}
    )
    stored = stored_tokens()
    response = callback(client, code="second-code", state=params["state"])
    assert response.status_code == 400
    assert len(fake.token_requests) == 1
    assert stored_tokens() == stored


@pytest.mark.parametrize("error", ["access_denied", "server_error"])
def test_an_error_parameter_is_a_400_saying_not_connected(sign_in, fake, client, error):
    seed_old_tokens()
    params = sign_in()
    response = callback(client, error=error, state=params["state"])
    assert_rejected_without_a_token_request(fake, response)
    assert "wasn't connected" in html.unescape(response.text)


def test_an_error_parameter_wins_over_a_code(sign_in, fake, client):
    seed_old_tokens()
    params = sign_in()
    response = callback(client, error="access_denied", code=CODE, state=params["state"])
    assert_rejected_without_a_token_request(fake, response)


# --- 7. Token request failure ---------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [invalid_grant, unauthorized, server_error, failing],
    ids=["invalid-grant", "unauthorized", "server-error", "transport-failure"],
)
def test_token_request_failure_is_a_502_and_changes_nothing(
    sign_in, fake, client, handler
):
    seed_old_tokens()
    fake.token_response = handler
    params = sign_in()
    response = callback(client, code=CODE, state=params["state"])
    assert response.status_code == 502
    assert "wasn't connected" in html.unescape(response.text)
    assert len(fake.token_requests) == 1
    assert stored_tokens() == [OLD_TOKENS]


def test_token_request_failure_with_no_prior_tokens_stores_none(sign_in, fake, client):
    fake.token_response = invalid_grant
    params = sign_in()
    assert callback(client, code=CODE, state=params["state"]).status_code == 502
    assert stored_tokens() == []


# --- 8. Atomic write ------------------------------------------------------------------------


def test_the_same_state_cannot_store_tokens_twice(sign_in, fake, client):
    params = sign_in()
    first = callback(client, code=CODE, state=params["state"])
    second = callback(client, code=CODE, state=params["state"])
    assert (first.status_code, second.status_code) == (200, 400)
    assert len(stored_tokens()) == 1


def test_each_authorization_consumes_only_its_own_state(sign_in, fake, client):
    first, second = sign_in(), sign_in()
    assert callback(client, code=CODE, state=second["state"]).status_code == 200
    assert callback(client, code=CODE, state=first["state"]).status_code == 200


# --- 9. Nothing secret is printed or logged -------------------------------------------------


def test_no_credential_is_printed_or_logged(sign_in, fake, client, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    params = sign_in()
    response = callback(client, code=CODE, state=params["state"])
    verifier = form(fake.token_requests[0])["code_verifier"]

    seen = capsys.readouterr()
    everything = seen.out + seen.err + logged(caplog) + response.text
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CODE, verifier):
        assert secret not in everything


def test_a_failed_exchange_does_not_print_or_log_credentials(
    sign_in, fake, client, capsys, caplog
):
    caplog.set_level(logging.DEBUG)
    fake.token_response = invalid_grant
    params = sign_in()
    response = callback(client, code=CODE, state=params["state"])
    verifier = form(fake.token_requests[0])["code_verifier"]

    seen = capsys.readouterr()
    everything = seen.out + seen.err + logged(caplog) + response.text
    for secret in (CODE, verifier):
        assert secret not in everything


def test_the_access_log_does_not_carry_the_authorization_code(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:50000",
        "GET",
        f"/oauth/sentience/callback?code={CODE}&state=state-example",
        "1.1",
        200,
    )
    assert CODE not in caplog.text


# --- 10. One shared httpx client, replaceable through its transport -------------------------


def test_every_request_to_the_auth_server_goes_through_the_injected_transport(
    sign_in, fake, client
):
    params = sign_in()
    callback(client, code=CODE, state=params["state"])
    assert [(r.method, r.url.path) for r in fake.requests] == [
        ("POST", "/oauth/mcp-issuer/register"),
        ("POST", "/oauth/mcp-issuer/token"),
    ]
    assert {r.url.host for r in fake.requests} == {"auth.example.com"}


def test_the_shared_client_uses_the_injected_transport(fake):
    with http.client() as shared:
        assert shared.get(f"{ISSUER}/anywhere").status_code == 404
    assert len(fake.requests) == 1


# --- 11. Authorization-server issuer on the callback (RFC 9207, issue #14) -------------------


@pytest.mark.parametrize(
    "iss",
    [
        "https://evil.example.org",
        "https://auth.example.com.evil.example.org",
        "https://auth.example.com/extra",
        "http://auth.example.com",
        "https://AUTH.example.com",
        "https://auth.example.co",
        "",
    ],
    ids=[
        "other-host",
        "issuer-as-prefix-of-other-host",
        "issuer-with-path",
        "other-scheme",
        "other-case",
        "truncated-issuer",
        "empty",
    ],
)
def test_a_callback_whose_issuer_differs_is_a_400_with_no_token_request(
    sign_in, fake, client, iss
):
    seed_old_tokens()
    params = sign_in()
    response = callback(client, code=CODE, state=params["state"], iss=iss)
    assert_rejected_without_a_token_request(fake, response)
    assert "wasn't connected" in html.unescape(response.text)


def test_a_callback_with_the_configured_issuer_completes_the_exchange(
    sign_in, fake, client
):
    params = sign_in()
    response = callback(client, code=CODE, state=params["state"], iss=ISSUER)
    assert response.status_code == 200
    assert len(fake.token_requests) == 1
    [(client_id, access, refresh, _, _)] = stored_tokens()
    assert (client_id, access, refresh) == (CLIENT_ID, ACCESS_TOKEN, REFRESH_TOKEN)


def test_a_callback_without_an_issuer_still_completes_the_exchange(
    sign_in, fake, client
):
    params = sign_in()
    response = callback(client, code=CODE, state=params["state"])
    assert response.status_code == 200
    assert len(fake.token_requests) == 1


def test_the_issuer_is_checked_against_the_configured_setting(
    fake, monkeypatch, browser, client
):
    monkeypatch.setenv("SENTIENCE_AUTH_ISSUER", "https://other-auth.example.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)
    cli.main(["auth", "sentience"])
    params = {k: v[0] for k, v in parse_qs(urlsplit(browser[-1]).query).items()}
    rejected = callback(client, code=CODE, state=params["state"], iss=ISSUER)
    assert rejected.status_code == 400
    assert fake.token_requests == []
    accepted = callback(
        client,
        code=CODE,
        state=params["state"],
        iss="https://other-auth.example.com",
    )
    assert accepted.status_code == 200
    assert len(fake.token_requests) == 1


def test_a_mismatched_issuer_with_an_error_parameter_is_still_a_400(
    sign_in, fake, client
):
    seed_old_tokens()
    params = sign_in()
    response = callback(
        client,
        error="access_denied",
        state=params["state"],
        iss="https://evil.example.org",
    )
    assert_rejected_without_a_token_request(fake, response)


def test_a_mismatched_issuer_sends_no_request_to_the_auth_server_at_all(
    sign_in, fake, client
):
    params = sign_in()
    sent_before = len(fake.requests)
    callback(client, code=CODE, state=params["state"], iss="https://evil.example.org")
    assert len(fake.requests) == sent_before
