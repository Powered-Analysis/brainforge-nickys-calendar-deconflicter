"""Issue #8: refreshing Sentience access, the Re-Authorization Alert, and recovery.

Sentience's auth server and Resend are replaced by one injected `httpx` transport that routes by
host. The Sentience MCP session is replaced at the one function that opens it and dispatches a
payload, Google by the fake the dispatch tests use. The database is the libSQL container.
"""

import base64
import hashlib
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from dispatcher import db, google_calendar, http, sentience_auth, sentience_mcp
from dispatcher.app import app
from test_calendar_attach import (  # noqa: F401 (fixtures)
    BASE_URL,
    HOME,
    SIDE,
    WORK,
    database,
    execute,
    sentience,
    settings_env,
)
from test_calendar_attach import seed as seed_calendar
from test_conflict_dispatch import (  # noqa: F401 (fixtures)
    NEXT_TOKEN,
    START_TOKEN,
    OverlapGoogle,
    meeting,
    operator,
    processed,
    world,
)
from test_gcal_webhook import (
    HOME_CHANNEL,
    SIDE_CHANNEL,
    WORK_CHANNEL,
    Read,
    notify,
    token_of,
)

ISSUER = "https://auth.example.com"
STORED_CLIENT = "client-example"
OLD_ACCESS = "old-access-token-example-111"
OLD_REFRESH = "old-refresh-token-example-222"
NEW_ACCESS = "new-access-token-example-333"
NEW_REFRESH = "new-refresh-token-example-444"
RESEND_KEY = "resend-key-example-555"
SECRET = "renewal-secret-example-value"
ALERT_TO = "operator-inbox@example.com"
EXPECTED_SENDER = "Calendar Deconflicter <onboarding@resend.dev>"
EXPECTED_SUBJECT = (
    "[Calendar Deconflicter] Alert \N{EM DASH} Sentience needs re-authorization"
)
REAUTH_LINK = f"{BASE_URL}/oauth/sentience/reauth"
SECRETS = (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH, RESEND_KEY)
FAR_FUTURE = 4102444800
STOP_PATH = "/calendar/v3/channels/stop"
AUTH = {"Authorization": f"Bearer {SECRET}"}


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def tokens_ok(request):
    return httpx.Response(
        200,
        json={
            "access_token": NEW_ACCESS,
            "refresh_token": NEW_REFRESH,
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )


def invalid_grant_400(request):
    return httpx.Response(400, json={"error": "invalid_grant"})


def invalid_grant_401(request):
    return httpx.Response(401, json={"error": "invalid_grant"})


def failing_transport(request):
    raise httpx.ConnectError("unreachable", request=request)


class Network:
    """Sentience's auth server and Resend, routed by host. Every request is recorded."""

    def __init__(self):
        self.requests = []
        self.token_response = tokens_ok
        self.resend_response = lambda request: httpx.Response(200, json={"id": "x"})

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "api.resend.com":
            return self.resend_response(request)
        if (
            request.url.host == "auth.example.com"
            and request.url.path == "/oauth/mcp-issuer/token"
        ):
            return self.token_response(request)
        return httpx.Response(404)

    @property
    def refreshes(self):
        return [
            r
            for r in self.requests
            if r.url.path == "/oauth/mcp-issuer/token"
            and form(r).get("grant_type") == "refresh_token"
        ]

    @property
    def emails(self):
        return [r for r in self.requests if r.url.host == "api.resend.com"]


class Mcp:
    """The Sentience MCP session: records each dispatch; tokens in `rejected` get a 401."""

    def __init__(self):
        self.calls = []
        self.rejected = set()
        self.fail_with = None

    def __call__(self, access_token, message):
        self.calls.append((access_token, message))
        if access_token in self.rejected:
            raise sentience_mcp.AccessTokenRejected("fictional 401")
        if self.fail_with is not None:
            raise self.fail_with

    @property
    def tokens(self):
        return [token for token, _ in self.calls]


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def calendars():
    seed_calendar(WORK, 1, WORK_CHANNEL, START_TOKEN)
    seed_calendar(HOME, 1, HOME_CHANNEL, "home-token-0")
    seed_calendar(SIDE, 0, SIDE_CHANNEL, "side-token-0")


@pytest.fixture
def google(monkeypatch):
    fake = OverlapGoogle()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


@pytest.fixture
def net(monkeypatch):
    network = Network()
    monkeypatch.setattr(http, "transport", httpx.MockTransport(network))
    return network


@pytest.fixture
def mcp(monkeypatch):
    fake = Mcp()
    monkeypatch.setattr(sentience_mcp, "dispatch", fake)
    return fake


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("SENTIENCE_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("RESEND_API_KEY", RESEND_KEY)
    monkeypatch.setenv("ALERT_TO", ALERT_TO)
    monkeypatch.setenv("INTERNAL_CRON_SECRET", SECRET)
    execute("DELETE FROM sentience_client")
    execute(
        "INSERT INTO sentience_client (id, client_id) VALUES ('sentience', ?)",
        (STORED_CLIENT,),
    )
    yield
    for table in (
        "sentience_auth",
        "sentience_auth_expired",
        "sentience_client",
        "sentience_pending_authorization",
    ):
        execute(f"DELETE FROM {table}")


def store_auth(expires_at=FAR_FUTURE, access=OLD_ACCESS, refresh=OLD_REFRESH):
    execute("DELETE FROM sentience_auth")
    execute(
        "INSERT INTO sentience_auth (id, client_id, access_token, refresh_token, expires_at,"
        " updated_at) VALUES ('sentience', ?, ?, ?, ?, 1700000000)",
        (STORED_CLIENT, access, refresh, expires_at),
    )


def lapsed():
    store_auth(expires_at=int(time.time()) - 10)


def stored_auth():
    rows = execute(
        "SELECT client_id, access_token, refresh_token, expires_at, updated_at"
        " FROM sentience_auth"
    )
    return [tuple(r) for r in rows]


def expired():
    return db.is_sentience_auth_expired()


def expire_now():
    store_auth()
    db.mark_sentience_auth_expired(int(time.time()))


def conflict(google):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )


def later_conflict(google, token=NEXT_TOKEN, next_token="work-token-2"):
    """A second change on the work calendar that overlaps another event, on another day."""
    changed = meeting("later", "10:00", "11:00", day="2099-04-02")
    partner = meeting("partner", "10:30", "11:30", day="2099-04-02")
    google.script[(WORK, token)] = Read([[changed]], next_token)
    google.stored[WORK] = [changed, partner]


def quiet_delta(google, calendar=WORK, token=START_TOKEN, next_token=NEXT_TOKEN):
    google.script[(calendar, token)] = Read([[]], next_token)


def quiet_home(google):
    quiet_delta(google, HOME, "home-token-0", "home-token-1")


def reconnect(client):
    """The operator follows the alert's link and signs in again; returns the callback's response."""
    redirect = client.get("/oauth/sentience/reauth", follow_redirects=False)
    assert redirect.status_code in (302, 303, 307)
    state = parse_qs(urlsplit(redirect.headers["location"]).query)["state"][0]
    return client.get(
        "/oauth/sentience/callback", params={"code": "code-example", "state": state}
    )


# --- Whether a token needs refreshing (criterion 1) ----------------------------------------


@pytest.mark.parametrize(
    ("lifetime", "stale"),
    [
        (-1000, True),
        (-1, True),
        (0, True),
        (30, True),
        (59, True),
        (61, False),
        (3600, False),
    ],
)
def test_a_token_is_refreshed_when_lapsed_or_lapsing_within_a_minute(lifetime, stale):
    now = 1_800_000_000
    assert sentience_auth.needs_refresh(now + lifetime, now) is stale


# --- 1. Refresh before a dispatch ------------------------------------------------------------


def test_a_token_good_for_longer_than_a_minute_is_used_without_a_refresh(
    client, google, calendars, net, mcp
):
    store_auth(expires_at=int(time.time()) + 600)
    conflict(google)
    assert notify(client).status_code == 200
    assert net.requests == []
    assert mcp.tokens == [OLD_ACCESS]


@pytest.mark.parametrize(
    "seconds", [-3600, -1, 20], ids=["long-ago", "just-lapsed", "in-20s"]
)
def test_a_lapsed_or_lapsing_token_is_refreshed_before_the_dispatch(
    client, google, calendars, net, mcp, seconds
):
    store_auth(expires_at=int(time.time()) + seconds)
    conflict(google)
    assert notify(client).status_code == 200
    assert len(net.refreshes) == 1
    assert mcp.tokens == [NEW_ACCESS]


def test_a_delta_with_no_conflict_does_not_refresh(client, google, calendars, net, mcp):
    lapsed()
    quiet_delta(google)
    assert notify(client).status_code == 200
    assert net.requests == []
    assert mcp.calls == []


# --- 2. The refresh request and what it stores -------------------------------------------------


def test_the_refresh_posts_the_three_form_fields_to_the_token_endpoint(
    client, google, calendars, net, mcp
):
    lapsed()
    conflict(google)
    notify(client)
    request = net.refreshes[0]
    assert request.method == "POST"
    assert str(request.url) == f"{ISSUER}/oauth/mcp-issuer/token"
    assert request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    assert form(request) == {
        "grant_type": "refresh_token",
        "refresh_token": OLD_REFRESH,
        "client_id": STORED_CLIENT,
    }


def test_a_refresh_stores_the_new_access_token_its_expiry_and_the_replacement_refresh_token(
    client, google, calendars, net, mcp
):
    lapsed()
    conflict(google)
    before = int(time.time())
    notify(client)
    after = int(time.time())
    ((client_id, access, refresh, expires_at, updated_at),) = stored_auth()
    assert (client_id, access, refresh) == (STORED_CLIENT, NEW_ACCESS, NEW_REFRESH)
    assert before + 3600 <= expires_at <= after + 3600
    assert before <= updated_at <= after


def test_without_a_replacement_the_old_refresh_token_is_kept(
    client, google, calendars, net, mcp
):
    net.token_response = lambda request: httpx.Response(
        200, json={"access_token": NEW_ACCESS, "expires_in": 3600}
    )
    lapsed()
    conflict(google)
    notify(client)
    ((_, access, refresh, _, _),) = stored_auth()
    assert (access, refresh) == (NEW_ACCESS, OLD_REFRESH)


def test_the_next_refresh_uses_the_replacement_refresh_token(
    client, google, calendars, net, mcp
):
    lapsed()
    conflict(google)
    notify(client)
    execute("UPDATE sentience_auth SET expires_at = 1")
    later_conflict(google)
    assert notify(client).status_code == 200
    assert form(net.refreshes[1])["refresh_token"] == NEW_REFRESH


@pytest.mark.parametrize(
    "response",
    [
        lambda request: httpx.Response(200, json={"refresh_token": NEW_REFRESH}),
        lambda request: httpx.Response(200, text="not json"),
        lambda request: httpx.Response(500, text="boom"),
    ],
    ids=["no-access-token", "not-json", "server-error"],
)
def test_a_refresh_that_returns_no_usable_tokens_changes_nothing_stored(
    client, google, calendars, net, mcp, response
):
    net.token_response = response
    lapsed()
    before = stored_auth()
    conflict(google)
    assert notify(client).status_code == 503
    assert stored_auth() == before
    assert mcp.calls == []


@pytest.mark.parametrize(
    "lifetime", [None, 30], ids=["no-lifetime-stated", "lifetime-under-the-margin"]
)
def test_a_token_that_lapses_within_a_minute_is_refreshed_again_before_its_next_use(
    client, google, calendars, net, mcp, lifetime
):
    payload = {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH}
    if lifetime is not None:
        payload["expires_in"] = lifetime
    net.token_response = lambda request: httpx.Response(200, json=payload)
    lapsed()
    conflict(google)
    notify(client)
    later_conflict(google)
    assert notify(client).status_code == 200
    assert len(net.refreshes) == 2


def test_a_token_with_a_long_lifetime_is_not_refreshed_again_for_the_next_delivery(
    client, google, calendars, net, mcp
):
    lapsed()
    conflict(google)
    notify(client)
    later_conflict(google)
    assert notify(client).status_code == 200
    assert len(net.refreshes) == 1
    assert mcp.tokens == [NEW_ACCESS, NEW_ACCESS]


# --- 3. A rejected access token ------------------------------------------------------------


def test_a_401_from_the_mcp_server_refreshes_once_and_retries_once(
    client, google, calendars, net, mcp
):
    store_auth(expires_at=int(time.time()) + 600)
    mcp.rejected = {OLD_ACCESS}
    conflict(google)
    assert notify(client).status_code == 200
    assert len(net.refreshes) == 1
    assert mcp.tokens == [OLD_ACCESS, NEW_ACCESS]
    assert token_of(WORK) == NEXT_TOKEN
    assert len(processed()) == 1
    assert stored_auth()[0][1] == NEW_ACCESS


def test_a_second_401_leaves_the_conflict_undelivered_with_one_refresh_and_one_retry(
    client, google, calendars, net, mcp
):
    store_auth(expires_at=int(time.time()) + 600)
    mcp.rejected = {OLD_ACCESS, NEW_ACCESS}
    conflict(google)
    assert notify(client).status_code == 503
    assert len(net.refreshes) == 1
    assert len(mcp.calls) == 2
    assert token_of(WORK) == START_TOKEN
    assert processed() == {}
    assert not expired()
    assert net.emails == []


def test_a_failure_other_than_a_401_does_not_refresh(
    client, google, calendars, net, mcp
):
    store_auth(expires_at=int(time.time()) + 600)
    mcp.fail_with = sentience_mcp.DispatchFailed("fictional outage")
    conflict(google)
    assert notify(client).status_code == 503
    assert net.requests == []
    assert len(mcp.calls) == 1
    assert token_of(WORK) == START_TOKEN


def test_a_refresh_refused_after_a_401_marks_access_expired(
    client, google, calendars, net, mcp
):
    store_auth(expires_at=int(time.time()) + 600)
    mcp.rejected = {OLD_ACCESS}
    net.token_response = invalid_grant_400
    conflict(google)
    assert notify(client).status_code == 503
    assert expired()
    assert len(mcp.calls) == 1
    assert len(net.emails) == 1


# --- 4. Which refresh failures mark access expired ------------------------------------------


@pytest.mark.parametrize(
    "response", [invalid_grant_400, invalid_grant_401], ids=["status-400", "status-401"]
)
def test_invalid_grant_marks_access_expired_whatever_the_status(
    client, google, calendars, net, mcp, response
):
    net.token_response = response
    lapsed()
    conflict(google)
    assert notify(client).status_code == 503
    assert expired()
    assert mcp.calls == []
    assert token_of(WORK) == START_TOKEN
    assert processed() == {}


@pytest.mark.parametrize(
    "response",
    [
        failing_transport,
        lambda request: httpx.Response(500, text="boom"),
        lambda request: httpx.Response(503, json={"error": "temporarily_unavailable"}),
        lambda request: httpx.Response(400, json={"error": "invalid_request"}),
        lambda request: httpx.Response(401, json={"error": "invalid_client"}),
        lambda request: httpx.Response(400, text="invalid_grant"),
    ],
    ids=[
        "transport-error",
        "500",
        "503",
        "other-400",
        "other-401",
        "invalid-grant-only-in-plain-text",
    ],
)
def test_any_other_refresh_failure_is_undelivered_without_marking_access_expired(
    client, google, calendars, net, mcp, response
):
    net.token_response = response
    lapsed()
    conflict(google)
    assert notify(client).status_code == 503
    assert not expired()
    assert net.emails == []
    assert mcp.calls == []
    assert token_of(WORK) == START_TOKEN


def test_a_later_notification_delivers_once_the_refresh_works_again(
    client, google, calendars, net, mcp
):
    net.token_response = lambda request: httpx.Response(500)
    lapsed()
    conflict(google)
    assert notify(client).status_code == 503
    net.token_response = tokens_ok
    assert notify(client).status_code == 200
    assert mcp.tokens == [NEW_ACCESS]
    assert len(processed()) == 1


# --- 5. While access is expired --------------------------------------------------------------


@pytest.fixture
def lost(client, google, calendars, net, mcp):
    """Access has been refused, through a delivery that found Sentience refusing the token."""
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    assert notify(client).status_code == 503
    assert expired()
    net.requests.clear()
    return net


def test_while_expired_no_refresh_is_attempted_and_sentience_is_not_called(
    client, google, lost, mcp
):
    assert notify(client).status_code == 503
    assert lost.requests == []
    assert mcp.calls == []


def test_while_expired_even_a_valid_looking_token_is_not_used(
    client, google, lost, mcp
):
    store_auth(expires_at=FAR_FUTURE)
    assert notify(client).status_code == 503
    assert mcp.calls == []


def test_while_expired_every_conflict_is_undelivered_and_the_cursor_stays(
    client, google, lost, mcp
):
    for _ in range(2):
        assert notify(client).status_code == 503
        assert token_of(WORK) == START_TOKEN
        assert processed() == {}


def test_while_expired_a_delta_without_a_conflict_advances_the_cursor_and_answers_200(
    client, google, lost, mcp
):
    quiet_delta(google, WORK, START_TOKEN, "work-token-quiet")
    assert notify(client).status_code == 200
    assert token_of(WORK) == "work-token-quiet"
    assert lost.requests == []
    assert mcp.calls == []
    assert expired()


# --- 6. The Re-Authorization Alert ---------------------------------------------------------


@pytest.fixture
def alert(client, google, calendars, net, mcp):
    """The one request the dispatcher sent Resend when its access was refused."""
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    notify(client)
    (request,) = net.emails
    return request


def test_the_alert_is_a_post_to_resends_email_api_with_the_key(alert):
    assert alert.method == "POST"
    assert alert.url.host == "api.resend.com"
    assert alert.url.path == "/emails"
    assert alert.headers["authorization"] == f"Bearer {RESEND_KEY}"


def test_the_alert_has_the_exact_sender_recipient_and_subject(alert):
    body = json.loads(alert.content)
    assert body["from"] == EXPECTED_SENDER
    assert body["to"] in (ALERT_TO, [ALERT_TO])
    assert body["subject"] == EXPECTED_SUBJECT


def test_the_alert_body_is_one_sentence_with_the_reauthorization_link(alert):
    body = json.loads(alert.content)
    text = body.get("text") or re.sub(r"<[^>]+>", "", body["html"])
    assert REAUTH_LINK in text
    assert len(re.split(r"(?<=[.!?])\s+", text.strip())) == 1
    assert "not being delivered" in text.lower() or "not delivered" in text.lower()


def test_the_alert_carries_no_event_data_and_no_token(alert):
    content = alert.content.decode()
    for fragment in (
        "Fictional",
        "changed",
        "other",
        "sam@example.com",
        "operator@example.com",
    ):
        assert fragment not in content
    for secret in (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH):
        assert secret not in content


def test_the_link_follows_the_public_base_url(
    monkeypatch, client, google, calendars, net, mcp
):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://other-example.fly.dev")
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    notify(client)
    content = net.emails[0].content.decode()
    assert "https://other-example.fly.dev/oauth/sentience/reauth" in content


# --- 7. One email per expiry ------------------------------------------------------------------


def test_no_further_email_is_sent_while_access_stays_expired(
    client, google, calendars, net, mcp
):
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    for _ in range(3):
        notify(client)
    notify(client, HOME_CHANNEL)
    assert len(net.emails) == 1


@pytest.mark.parametrize(
    "resend",
    [
        lambda request: httpx.Response(500, text="boom"),
        lambda request: httpx.Response(403, json={"message": "bad key"}),
        failing_transport,
    ],
    ids=["500", "403", "transport-error"],
)
def test_a_failed_send_changes_nothing_else_and_is_not_retried(
    client, google, calendars, net, mcp, resend
):
    net.resend_response = resend
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    assert notify(client).status_code == 503
    assert expired()
    assert len(net.emails) == 1
    assert notify(client).status_code == 503
    assert expired()
    assert len(net.emails) == 1


@pytest.mark.parametrize("name", ["RESEND_API_KEY", "ALERT_TO"])
def test_an_unset_alert_setting_is_a_failed_send_not_a_crash(
    monkeypatch, client, google, calendars, net, mcp, name
):
    monkeypatch.delenv(name)
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    assert notify(client).status_code == 503
    assert expired()
    assert net.emails == []


def test_a_failed_send_is_logged(caplog, client, google, calendars, net, mcp):
    net.resend_response = lambda request: httpx.Response(500, text="boom")
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    with caplog.at_level(logging.DEBUG):
        notify(client)
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_after_a_recovery_the_next_expiry_sends_again(
    client, google, calendars, net, mcp
):
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    notify(client)
    assert len(net.emails) == 1
    net.token_response = tokens_ok
    quiet_home(google)
    assert reconnect(client).status_code == 200
    assert not expired()
    assert len(net.emails) == 1
    # The delta the recovery re-read stored a new cursor; Sentience now refuses the next refresh.
    execute("UPDATE sentience_auth SET expires_at = 1")
    net.token_response = invalid_grant_400
    later_conflict(google, token=token_of(WORK))
    assert notify(client).status_code == 503
    assert expired()
    assert len(net.emails) == 2


# --- 8. The renewal endpoint -------------------------------------------------------------------


class GoogleWithStop(OverlapGoogle):
    def route(self, r):
        if r["path"] == STOP_PATH and r["method"] == "POST":
            return 204, {}
        return super().route(r)

    @property
    def stops(self):
        return [r for r in self.api_requests if r["path"] == STOP_PATH]


@pytest.fixture
def renewing_google(monkeypatch):
    fake = GoogleWithStop()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


def test_while_expired_the_renewal_endpoint_still_renews_every_channel_then_answers_503(
    client, renewing_google, calendars, net
):
    expire_now()
    response = client.post("/internal/renew-watches", headers=AUTH)
    assert response.status_code == 503
    assert {r["path"].split("/")[4] for r in renewing_google.watches} == {WORK, HOME}
    assert len(renewing_google.stops) == 2
    assert net.requests == []


def test_the_renewal_endpoint_answers_200_when_access_is_not_expired(
    client, renewing_google, calendars, net
):
    store_auth()
    assert client.post("/internal/renew-watches", headers=AUTH).status_code == 200


def test_while_expired_with_nothing_to_renew_the_endpoint_still_answers_503(
    client, renewing_google, net
):
    expire_now()
    assert client.post("/internal/renew-watches", headers=AUTH).status_code == 503
    assert renewing_google.requests == []


def test_while_expired_an_unauthenticated_ping_still_gets_401(
    client, renewing_google, calendars
):
    expire_now()
    assert client.post("/internal/renew-watches").status_code == 401
    assert renewing_google.requests == []


def test_a_renewal_after_recovery_answers_200_again(
    client, renewing_google, calendars, net, mcp
):
    expire_now()
    quiet_delta(renewing_google)
    quiet_home(renewing_google)
    assert reconnect(client).status_code == 200
    assert client.post("/internal/renew-watches", headers=AUTH).status_code == 200


# --- 9. GET /oauth/sentience/reauth -----------------------------------------------------------


def authorize_params(response):
    assert response.status_code in (302, 303, 307)
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        f"{ISSUER}/oauth/mcp-issuer/authorize"
    )
    return {k: v[0] for k, v in parse_qs(location.query).items()}


def test_while_expired_the_link_redirects_to_a_new_authorization_with_the_stored_client(
    client, net
):
    expire_now()
    params = authorize_params(
        client.get("/oauth/sentience/reauth", follow_redirects=False)
    )
    assert params["client_id"] == STORED_CLIENT
    assert params["response_type"] == "code"
    assert params["scope"] == "sentience"
    assert params["redirect_uri"] == f"{BASE_URL}/oauth/sentience/callback"
    assert params["code_challenge_method"] == "S256"
    assert params["state"]
    assert net.requests == []


def test_the_link_stores_the_verifier_that_matches_the_challenge_under_the_state(
    client, net
):
    expire_now()
    params = authorize_params(
        client.get("/oauth/sentience/reauth", follow_redirects=False)
    )
    verifier = db.get_pending_code_verifier(params["state"])
    assert verifier
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert (
        base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        == params["code_challenge"]
    )


def test_each_visit_starts_a_new_authorization(client, net):
    expire_now()
    first = authorize_params(
        client.get("/oauth/sentience/reauth", follow_redirects=False)
    )
    second = authorize_params(
        client.get("/oauth/sentience/reauth", follow_redirects=False)
    )
    assert first["state"] != second["state"]
    assert first["code_challenge"] != second["code_challenge"]
    assert db.get_pending_code_verifier(first["state"]) != db.get_pending_code_verifier(
        second["state"]
    )


def test_while_connected_the_link_starts_nothing_and_answers_409(client, net):
    store_auth()
    response = client.get("/oauth/sentience/reauth", follow_redirects=False)
    assert response.status_code == 409
    assert "location" not in response.headers
    assert response.headers["content-type"].startswith("text/html")
    assert response.text.strip()
    assert execute("SELECT * FROM sentience_pending_authorization") == []
    assert net.requests == []


def test_when_never_connected_the_link_starts_nothing_and_answers_409(client, net):
    execute("DELETE FROM sentience_client")
    response = client.get("/oauth/sentience/reauth", follow_redirects=False)
    assert response.status_code == 409
    assert execute("SELECT * FROM sentience_pending_authorization") == []
    assert execute("SELECT * FROM sentience_client") == []
    assert net.requests == []


def test_the_link_stops_working_once_the_operator_has_reconnected(
    client, net, mcp, google, calendars
):
    expire_now()
    quiet_delta(google)
    quiet_home(google)
    assert reconnect(client).status_code == 200
    assert (
        client.get("/oauth/sentience/reauth", follow_redirects=False).status_code == 409
    )


def test_the_link_answers_409_after_a_refresh_that_worked(
    client, google, calendars, net, mcp
):
    lapsed()
    conflict(google)
    assert notify(client).status_code == 200
    assert (
        client.get("/oauth/sentience/reauth", follow_redirects=False).status_code == 409
    )


def test_the_link_works_after_a_delivery_found_invalid_grant(
    client, google, calendars, net, mcp
):
    net.token_response = invalid_grant_401
    lapsed()
    conflict(google)
    notify(client)
    authorize_params(client.get("/oauth/sentience/reauth", follow_redirects=False))


# --- 10. Recovery ------------------------------------------------------------------------------


def test_a_successful_callback_clears_the_mark_and_delivers_what_went_undelivered(
    client, google, calendars, net, mcp
):
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    quiet_home(google)
    assert notify(client).status_code == 503
    assert mcp.calls == []
    net.token_response = tokens_ok
    assert reconnect(client).status_code == 200
    assert not expired()
    assert mcp.tokens == [NEW_ACCESS]
    assert len(processed()) == 1
    assert token_of(WORK) == NEXT_TOKEN


def test_recovery_processes_every_active_calendar_and_no_inactive_one(
    client, google, calendars, net, mcp
):
    expire_now()
    quiet_delta(google, WORK, START_TOKEN, "work-token-1")
    quiet_home(google)
    assert reconnect(client).status_code == 200
    assert token_of(WORK) == "work-token-1"
    assert token_of(HOME) == "home-token-1"
    assert token_of(SIDE) == "side-token-0"


def test_recovery_does_not_send_a_conflict_that_was_already_delivered(
    client, google, calendars, net, mcp
):
    store_auth(expires_at=int(time.time()) + 600)
    conflict(google)
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    # A second change arrives while access is lost; its delta also re-lists the first.
    first = meeting("changed", "10:00", "11:00")
    second = meeting("second", "13:00", "14:00")
    partner = meeting("partner", "13:30", "14:30")
    google.script[(WORK, NEXT_TOKEN)] = Read([[first, second]], "work-token-2")
    google.stored[WORK] = [first, second, partner, meeting("other", "10:30", "11:30")]
    db.mark_sentience_auth_expired(int(time.time()))
    assert notify(client).status_code == 503
    assert len(mcp.calls) == 1
    quiet_home(google)
    assert reconnect(client).status_code == 200
    assert len(mcp.calls) == 2
    assert len(processed()) == 2


def test_a_failed_token_exchange_leaves_access_expired_and_delivers_nothing(
    client, google, calendars, net, mcp
):
    expire_now()
    conflict(google)
    net.token_response = invalid_grant_400
    assert reconnect(client).status_code >= 400
    assert expired()
    assert mcp.calls == []
    assert token_of(WORK) == START_TOKEN


def test_a_calendar_that_fails_does_not_stop_the_others_being_recovered(
    client, google, calendars, net, mcp
):
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    notify(client)
    net.token_response = tokens_ok
    google.script[(HOME, "home-token-0")] = 503
    reconnect(client)
    assert not expired()
    assert len(mcp.calls) == 1
    assert token_of(WORK) == NEXT_TOKEN
    assert token_of(HOME) == "home-token-0"


def test_the_first_connection_also_processes_every_active_calendars_delta(
    client, google, calendars, net, mcp
):
    execute("DELETE FROM sentience_auth")
    conflict(google)
    quiet_home(google)
    db.save_pending_authorization(
        "state-example", "verifier-example-" * 4, int(time.time())
    )
    response = client.get(
        "/oauth/sentience/callback",
        params={"code": "code-example", "state": "state-example"},
    )
    assert response.status_code == 200
    assert mcp.tokens == [NEW_ACCESS]
    assert token_of(WORK) == NEXT_TOKEN
    assert token_of(HOME) == "home-token-1"


# --- 11. Nothing secret is printed or logged --------------------------------------------------


def assert_no_secret(text):
    for secret in SECRETS:
        assert secret not in text


@pytest.mark.parametrize(
    "token_response",
    [
        tokens_ok,
        invalid_grant_400,
        invalid_grant_401,
        failing_transport,
        lambda request: httpx.Response(500, text="boom"),
    ],
    ids=["success", "invalid-grant-400", "invalid-grant-401", "transport-error", "500"],
)
@pytest.mark.parametrize(
    "resend_response",
    [
        lambda request: httpx.Response(200, json={"id": "x"}),
        lambda request: httpx.Response(500, text="boom"),
        failing_transport,
    ],
    ids=["sent", "send-500", "send-transport-error"],
)
def test_no_token_or_key_reaches_a_log_the_output_or_a_response(
    caplog, capsys, client, google, calendars, net, mcp, token_response, resend_response
):
    net.token_response = token_response
    net.resend_response = resend_response
    mcp.rejected = {OLD_ACCESS, NEW_ACCESS}
    lapsed()
    conflict(google)
    with caplog.at_level(logging.DEBUG):
        responses = [notify(client), notify(client)]
        responses.append(client.post("/internal/renew-watches", headers=AUTH))
        responses.append(client.get("/oauth/sentience/reauth", follow_redirects=False))
    captured = capsys.readouterr()
    assert_no_secret("\n".join(r.getMessage() for r in caplog.records))
    assert_no_secret(captured.out)
    assert_no_secret(captured.err)
    for response in responses:
        assert_no_secret(response.text)
        assert_no_secret(str(response.headers))


def test_the_reauthorization_redirect_and_callback_leak_no_stored_token(
    caplog, client, google, calendars, net, mcp
):
    expire_now()
    quiet_delta(google)
    quiet_home(google)
    with caplog.at_level(logging.DEBUG):
        response = reconnect(client)
    assert_no_secret(response.text)
    assert_no_secret("\n".join(r.getMessage() for r in caplog.records))


# --- 12. Resend is called for nothing but the alert --------------------------------------------


def test_without_an_expiry_resend_is_never_called(client, google, calendars, net, mcp):
    lapsed()
    conflict(google)
    assert notify(client).status_code == 200
    mcp.rejected = {NEW_ACCESS}
    later_conflict(google)
    assert notify(client).status_code == 503
    assert net.emails == []
    assert client.post("/internal/renew-watches", headers=AUTH).status_code in (
        200,
        502,
    )
    assert net.emails == []


def test_every_outside_request_goes_to_the_auth_server_or_to_resend_for_the_alert(
    client, google, calendars, net, mcp
):
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    notify(client)
    notify(client)
    net.token_response = tokens_ok
    quiet_home(google)
    reconnect(client)
    assert {r.url.host for r in net.requests} <= {"auth.example.com", "api.resend.com"}
    assert [(r.method, r.url.path) for r in net.emails] == [("POST", "/emails")]


def test_a_lost_and_recovered_access_makes_no_write_to_google(
    client, google, calendars, net, mcp
):
    net.token_response = invalid_grant_400
    lapsed()
    conflict(google)
    notify(client)
    net.token_response = tokens_ok
    quiet_home(google)
    reconnect(client)
    assert google.writes == []
