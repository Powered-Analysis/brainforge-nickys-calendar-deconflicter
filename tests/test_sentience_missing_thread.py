"""Issue #70: a missing or empty SENTIENCE_THREAD_ID is a dispatch failure, found before token work.

Dispatch Tool Call: the dispatcher never calls `ask_sentience` without a `thread_id`.
Delivery: a missing or empty `SENTIENCE_THREAD_ID` is a failed delivery, so the conflict is not
recorded, the calendar's `syncToken` is not advanced, and the webhook answers `503`. The
dispatcher reports it before it renews any access, so no token refresh happens for that delivery.
Sentience's auth server is replaced by an injected `httpx2` transport, the MCP session at the one
function that dispatches, Google by the fake the dispatch tests use; the database is libSQL.
"""

# ruff: noqa: F811 (tests take the imported fixtures by name)

import pytest

from dispatcher import sentience_auth, sentience_mcp
from dispatcher.errors import OperatorError
from test_calendar_attach import (  # noqa: F401 (fixtures)
    database,
    execute,
    sentience,
    settings_env,
)
from test_conflict_dispatch import (  # noqa: F401 (fixtures)
    NEXT_TOKEN,
    START_TOKEN,
    WORK,
    operator,
    processed,
)
from test_gcal_webhook import notify, token_of
from test_sentience_refresh import (  # noqa: F401 (fixtures)
    NEW_ACCESS,
    OLD_ACCESS,
    OLD_REFRESH,
    calendars,
    client,
    conflict,
    environment,
    expire_now,
    expired,
    google,
    lapsed,
    mcp,
    net,
    store_auth,
    stored_auth,
)

THREAD_ID = "thread-example-0001"
MESSAGE = "a conflict message"
UNSET_OR_EMPTY = pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])


@pytest.fixture
def no_thread(monkeypatch, mcp, value):
    """SENTIENCE_THREAD_ID unset or empty, after the `mcp` fixture has set it."""
    if value is None:
        monkeypatch.delenv("SENTIENCE_THREAD_ID", raising=False)
    else:
        monkeypatch.setenv("SENTIENCE_THREAD_ID", value)


# --- The type of the failure (acceptance 1) --------------------------------------------------


@UNSET_OR_EMPTY
def test_a_missing_thread_id_raises_dispatch_failed(no_thread, net, mcp):
    store_auth()
    with pytest.raises(sentience_mcp.DispatchFailed) as raised:
        sentience_auth.deliver(MESSAGE)
    assert "SENTIENCE_THREAD_ID" in str(raised.value)
    assert OLD_ACCESS not in str(raised.value)
    assert OLD_REFRESH not in str(raised.value)


@UNSET_OR_EMPTY
def test_dispatch_itself_raises_dispatch_failed_for_a_missing_thread_id(
    monkeypatch, value
):
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    if value is None:
        monkeypatch.delenv("SENTIENCE_THREAD_ID", raising=False)
    else:
        monkeypatch.setenv("SENTIENCE_THREAD_ID", value)
    with pytest.raises(sentience_mcp.DispatchFailed):
        sentience_mcp.dispatch(OLD_ACCESS, MESSAGE)


def test_dispatch_failed_is_still_an_operator_error():
    assert issubclass(sentience_mcp.DispatchFailed, OperatorError)


@UNSET_OR_EMPTY
def test_a_missing_thread_id_is_not_a_token_rejection(no_thread, net, mcp):
    # A plain dispatch failure: no retry with a fresh token.
    store_auth()
    with pytest.raises(sentience_mcp.DispatchFailed) as raised:
        sentience_auth.deliver(MESSAGE)
    assert not isinstance(raised.value, sentience_mcp.AccessTokenRejected)
    assert net.requests == []


# --- Detected before any token work (acceptance 2) -------------------------------------------


@UNSET_OR_EMPTY
def test_a_lapsed_token_is_not_refreshed_when_the_thread_id_is_missing(
    no_thread, net, mcp
):
    lapsed()
    before = stored_auth()
    with pytest.raises(sentience_mcp.DispatchFailed):
        sentience_auth.deliver(MESSAGE)
    assert net.requests == []
    assert mcp.calls == []
    assert stored_auth() == before


@UNSET_OR_EMPTY
def test_a_valid_token_is_not_used_when_the_thread_id_is_missing(no_thread, net, mcp):
    store_auth()
    with pytest.raises(sentience_mcp.DispatchFailed):
        sentience_auth.deliver(MESSAGE)
    assert net.requests == []
    assert mcp.calls == []


@UNSET_OR_EMPTY
def test_a_missing_thread_id_is_not_reported_as_lost_authorization(no_thread, net, mcp):
    lapsed()
    with pytest.raises(sentience_mcp.DispatchFailed):
        sentience_auth.deliver(MESSAGE)
    assert not expired()
    assert net.emails == []


# --- What still takes precedence -------------------------------------------------------------


@UNSET_OR_EMPTY
def test_nothing_stored_is_still_reported_as_not_connected(no_thread, net, mcp):
    execute("DELETE FROM sentience_auth")
    with pytest.raises(sentience_auth.NotConnected):
        sentience_auth.deliver(MESSAGE)
    assert net.requests == []
    assert mcp.calls == []


@UNSET_OR_EMPTY
def test_expired_access_is_still_reported_as_expired(no_thread, net, mcp):
    expire_now()
    with pytest.raises(sentience_auth.AuthExpired):
        sentience_auth.deliver(MESSAGE)
    assert net.requests == []
    assert mcp.calls == []


# --- Delivery's outcome through the webhook (acceptance 3) -----------------------------------


@UNSET_OR_EMPTY
def test_a_conflict_without_a_thread_id_is_not_recorded_and_answers_503_with_no_refresh(
    no_thread, client, google, calendars, net, mcp
):
    lapsed()
    before = stored_auth()
    conflict(google)
    assert notify(client).status_code == 503
    assert net.requests == []
    assert mcp.calls == []
    assert processed() == {}
    assert token_of(WORK) == START_TOKEN
    assert stored_auth() == before
    assert not expired()
    assert google.writes == []


@UNSET_OR_EMPTY
def test_the_held_back_conflict_is_delivered_once_the_thread_id_is_set(
    no_thread, client, google, calendars, net, mcp, monkeypatch
):
    lapsed()
    conflict(google)
    assert notify(client).status_code == 503
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    assert notify(client).status_code == 200
    assert mcp.tokens == [NEW_ACCESS]
    assert len(net.refreshes) == 1
    assert len(processed()) == 1
    assert token_of(WORK) == NEXT_TOKEN


# --- With the setting present, delivery is unchanged (acceptance 4) --------------------------


def test_a_valid_token_is_used_as_stored_when_the_thread_id_is_set(net, mcp):
    store_auth()
    sentience_auth.deliver(MESSAGE)
    assert mcp.calls == [(OLD_ACCESS, MESSAGE)]
    assert net.requests == []


def test_a_lapsed_token_is_refreshed_once_when_the_thread_id_is_set(net, mcp):
    lapsed()
    sentience_auth.deliver(MESSAGE)
    assert mcp.calls == [(NEW_ACCESS, MESSAGE)]
    assert len(net.refreshes) == 1


def test_a_rejected_token_is_still_retried_once_with_a_fresh_one(net, mcp):
    store_auth()
    mcp.rejected.add(OLD_ACCESS)
    sentience_auth.deliver(MESSAGE)
    assert mcp.tokens == [OLD_ACCESS, NEW_ACCESS]
    assert len(net.refreshes) == 1


def test_a_conflict_with_the_thread_id_set_is_recorded_and_answers_200(
    client, google, calendars, net, mcp
):
    store_auth()
    conflict(google)
    assert notify(client).status_code == 200
    assert len(mcp.calls) == 1
    assert len(processed()) == 1
    assert token_of(WORK) == NEXT_TOKEN


# --- The check that finds it ----------------------------------------------------------------


def test_the_configured_thread_id_is_returned(monkeypatch):
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    assert sentience_mcp.require_thread_id() == THREAD_ID


@UNSET_OR_EMPTY
def test_a_missing_thread_id_is_refused_by_the_check(no_thread):
    with pytest.raises(sentience_mcp.DispatchFailed):
        sentience_mcp.require_thread_id()


def test_the_check_reads_the_setting_each_time(monkeypatch):
    monkeypatch.setenv("SENTIENCE_THREAD_ID", "thread-example-a")
    assert sentience_mcp.require_thread_id() == "thread-example-a"
    monkeypatch.setenv("SENTIENCE_THREAD_ID", "")
    with pytest.raises(sentience_mcp.DispatchFailed):
        sentience_mcp.require_thread_id()
    monkeypatch.setenv("SENTIENCE_THREAD_ID", "thread-example-b")
    assert sentience_mcp.require_thread_id() == "thread-example-b"
