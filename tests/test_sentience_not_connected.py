"""Issue #30: one error type for "Sentience is not connected".

The condition is `NotConnected`, an `OperatorError`, whichever path finds nothing stored:
dispatching a message or renewing the tokens. Sentience's auth server and the MCP session are
replaced at their seams and must never be reached; the database is the libSQL container.
"""

import time

import httpx2
import pytest

from dispatcher import db, http, sentience_auth, sentience_mcp
from dispatcher.errors import OperatorError

MESSAGE = "conflict message example"
CLIENT = "client-example"
ACCESS = "access-token-example-aaa"
REFRESH = "refresh-token-example-bbb"
FAR_FUTURE = 4102444800


def execute(sql, params=()):
    conn = db.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def store_auth(expires_at=FAR_FUTURE):
    execute(
        "INSERT INTO sentience_auth (id, client_id, access_token, refresh_token, expires_at,"
        " updated_at) VALUES ('sentience', ?, ?, ?, ?, 1700000000)",
        (CLIENT, ACCESS, REFRESH, expires_at),
    )


@pytest.fixture(autouse=True)
def database():
    db.ensure_schema()
    for table in ("sentience_auth", "sentience_auth_expired", "sentience_client"):
        execute(f"DELETE FROM {table}")
    yield
    for table in ("sentience_auth", "sentience_auth_expired", "sentience_client"):
        execute(f"DELETE FROM {table}")


@pytest.fixture(autouse=True)
def outside_world(monkeypatch):
    """Records every Sentience auth request and MCP dispatch."""
    calls = {"auth": [], "mcp": []}

    def handler(request):
        calls["auth"].append(request)
        return httpx2.Response(500)

    def dispatch(access_token, message):
        calls["mcp"].append((access_token, message))

    monkeypatch.setattr(http, "transport", httpx2.MockTransport(handler))
    monkeypatch.setattr(sentience_mcp, "dispatch", dispatch)
    return calls


def test_not_connected_is_an_operator_error():
    assert issubclass(sentience_auth.NotConnected, OperatorError)


def test_deliver_with_nothing_stored_raises_not_connected(outside_world):
    with pytest.raises(sentience_auth.NotConnected, match="Sentience is not connected"):
        sentience_auth.deliver(MESSAGE)
    assert outside_world == {"auth": [], "mcp": []}


def test_refresh_with_nothing_stored_raises_not_connected(outside_world):
    with pytest.raises(sentience_auth.NotConnected, match="Sentience is not connected"):
        sentience_auth._refresh(int(time.time()))
    assert outside_world == {"auth": [], "mcp": []}


@pytest.mark.parametrize("path", ["deliver", "refresh"])
def test_both_paths_raise_the_same_type_and_message(path):
    def call():
        if path == "deliver":
            sentience_auth.deliver(MESSAGE)
        else:
            sentience_auth._refresh(int(time.time()))

    with pytest.raises(OperatorError) as raised:
        call()
    assert type(raised.value) is sentience_auth.NotConnected
    assert str(raised.value) == "Sentience is not connected"


def test_not_connected_is_not_a_dispatch_or_token_failure():
    with pytest.raises(OperatorError) as raised:
        sentience_auth.deliver(MESSAGE)
    assert not isinstance(raised.value, sentience_mcp.DispatchFailed)
    assert not isinstance(raised.value, sentience_auth.TokenRequestFailed)
    assert not isinstance(raised.value, sentience_auth.AuthExpired)


def test_expired_access_is_still_reported_as_expired_not_not_connected():
    store_auth()
    db.mark_sentience_auth_expired(int(time.time()))
    with pytest.raises(sentience_auth.AuthExpired):
        sentience_auth.deliver(MESSAGE)


def test_deliver_with_stored_tokens_dispatches(outside_world):
    store_auth()
    sentience_auth.deliver(MESSAGE)
    assert outside_world["mcp"] == [(ACCESS, MESSAGE)]
    assert outside_world["auth"] == []


def test_refresh_with_stored_tokens_does_not_raise_not_connected(
    outside_world, monkeypatch
):
    monkeypatch.setenv("SENTIENCE_AUTH_ISSUER", "https://auth.example.com")
    store_auth(expires_at=int(time.time()) - 10)
    with pytest.raises(sentience_auth.TokenRequestFailed):
        sentience_auth._refresh(int(time.time()))
    assert len(outside_world["auth"]) == 1
