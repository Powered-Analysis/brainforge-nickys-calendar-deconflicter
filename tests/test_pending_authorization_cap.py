"""Issue #29: the public reauth link cannot grow the pending-authorization table without limit.

The ruling: unfinished Sentience sign-ins are capped at the 20 most recent, the oldest are
discarded first, and the cap matters only while Sentience access has already expired.
Sentience's auth server is replaced by an injected `httpx2` transport (it is never needed,
because the client is already stored); the database is the libSQL container.
"""

import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient

from dispatcher import cli, db, http, sentience_auth
from dispatcher.app import app

ISSUER = "https://auth.example.com"
BASE_URL = "https://dispatcher-example.fly.dev"
CLIENT_ID = "client-example-123"
CODE = "authorization-code-example-ccc"
CAP = 20
REAUTH = "/oauth/sentience/reauth"


def execute(sql, params=()):
    conn = db.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def pending_states():
    return {r[0] for r in execute("SELECT state FROM sentience_pending_authorization")}


@pytest.fixture(autouse=True)
def database():
    db.ensure_schema()
    wipe()
    db.save_sentience_client_id(CLIENT_ID)
    yield
    wipe()


def wipe():
    for table in (
        "sentience_auth",
        "sentience_auth_expired",
        "sentience_client",
        "sentience_pending_authorization",
    ):
        execute(f"DELETE FROM {table}")


@pytest.fixture
def auth_server(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "access_token": "access-token-example",
                "refresh_token": "refresh-token-example",
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(http, "transport", httpx2.MockTransport(handler))
    return requests


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("SENTIENCE_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)


@pytest.fixture
def client(auth_server):
    with TestClient(app) as test_client:
        yield test_client


def expire_access():
    db.mark_sentience_auth_expired(int(time.time()))


def hit(client):
    return client.get(REAUTH, follow_redirects=False)


def state_of(response):
    assert response.status_code in (302, 303, 307)
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def seed(count, first_age=500):
    """`count` live pending rows; `s0` is the oldest and each next one is a second newer."""
    now = int(time.time())
    for i in range(count):
        db.save_pending_authorization(f"s{i}", "v" * 43, now - first_age + i)
    return [f"s{i}" for i in range(count)]


def test_the_link_never_leaves_more_than_20_rows_however_often_it_is_hit(client):
    expire_access()
    for _ in range(CAP + 10):
        assert hit(client).status_code in (302, 303, 307)
    assert len(pending_states()) == CAP


def test_the_cap_counts_the_row_being_stored_so_the_table_holds_exactly_20(client):
    expire_access()
    for hits in range(1, CAP + 3):
        hit(client)
        assert len(pending_states()) == min(hits, CAP)


def test_below_the_cap_every_authorization_is_kept(client):
    expire_access()
    states = {state_of(hit(client)) for _ in range(5)}
    assert len(states) == 5
    assert pending_states() == states


def test_the_oldest_row_is_discarded_first(client):
    expire_access()
    seeded = seed(CAP)
    new_state = state_of(hit(client))
    assert pending_states() == set(seeded[1:]) | {new_state}


def test_a_table_already_over_the_cap_is_cut_back_to_the_newest_20(client):
    expire_access()
    seeded = seed(CAP + 7)
    new_state = state_of(hit(client))
    assert pending_states() == set(seeded[-(CAP - 1) :]) | {new_state}


def test_the_newest_authorizations_survive_a_long_run_of_hits(client):
    expire_access()
    issued = [state_of(hit(client)) for _ in range(CAP + 15)]
    assert pending_states() == set(issued[-CAP:])


def test_a_discarded_authorization_can_no_longer_complete_but_a_kept_one_can(
    client, auth_server
):
    expire_access()
    issued = [state_of(hit(client)) for _ in range(CAP + 1)]
    evicted, kept = issued[0], issued[-1]
    rejected = client.get(
        "/oauth/sentience/callback", params={"code": CODE, "state": evicted}
    )
    assert rejected.status_code == 400
    assert auth_server == []
    accepted = client.get(
        "/oauth/sentience/callback", params={"code": CODE, "state": kept}
    )
    assert accepted.status_code == 200


def test_expired_rows_do_not_count_against_the_cap(client):
    expire_access()
    now = int(time.time())
    for i in range(CAP + 5):
        db.save_pending_authorization(f"stale{i}", "v" * 43, now - 10_000 + i)
    live = seed(CAP - 1)
    new_state = state_of(hit(client))
    assert pending_states() == set(live) | {new_state}


def test_rows_created_in_the_same_second_are_still_capped(auth_server):
    now = int(time.time())
    for _ in range(CAP + 10):
        with http.client() as sentience:
            sentience_auth.start_authorization(sentience, now)
    assert len(pending_states()) == CAP


def test_the_sign_in_command_is_capped_too(monkeypatch):
    monkeypatch.setattr(cli, "open_url", lambda url: None)
    for _ in range(CAP + 10):
        cli.main(["auth", "sentience"])
    assert len(pending_states()) == CAP


def test_the_link_and_the_sign_in_command_share_one_budget(client, monkeypatch):
    monkeypatch.setattr(cli, "open_url", lambda url: None)
    expire_access()
    for _ in range(CAP):
        hit(client)
    cli.main(["auth", "sentience"])
    assert len(pending_states()) == CAP


def test_while_access_is_current_the_link_is_a_409_and_stores_nothing(client):
    seeded = seed(3)
    response = hit(client)
    assert response.status_code == 409
    assert pending_states() == set(seeded)


def test_every_hit_while_expired_answers_a_redirect_not_an_error(client):
    expire_access()
    codes = {hit(client).status_code for _ in range(CAP + 5)}
    assert codes <= {302, 303, 307}


def test_the_cap_is_applied_without_any_request_to_the_auth_server(client, auth_server):
    expire_access()
    for _ in range(CAP + 5):
        hit(client)
    assert auth_server == []
