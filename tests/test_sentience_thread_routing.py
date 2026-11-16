"""Issue #66: every conflict is dispatched to the one Sentience chat named by SENTIENCE_THREAD_ID.

Dispatch Tool Call: each conflict is one `ask_sentience` call carrying `message` and
`thread_id`. Delivery: a missing or empty `SENTIENCE_THREAD_ID`, or an error result such as
Sentience rejecting the id, leaves the conflict unrecorded, the cursor where it was, and the
webhook answering 503. The MCP client is replaced where the session opens, so the real
`dispatch` runs; the webhook tests use the real database container.
"""

# ruff: noqa: F811 (tests take the imported fixtures by name)

import pytest

from conftest import ROOT
from dispatcher import sentience_mcp, settings
from dispatcher.errors import OperatorError
from test_calendar_attach import (  # noqa: F401 (fixtures)
    database,
    sentience,
    settings_env,
)
from test_conflict_dispatch import (  # noqa: F401 (fixtures)
    ACCESS_TOKEN,
    NEXT_TOKEN,
    START_TOKEN,
    WORK,
    calendars,
    client,
    google,
    meeting,
    notify,
    operator,
    payload_of,
    processed,
    token_of,
    world,
)

THREAD_ID = "thread-example-0001"


class Result:
    def __init__(self, is_error):
        self.is_error = is_error


class Session:
    """What the replaced MCP client records, and how it answers."""

    def __init__(self):
        self.calls = []
        self.result_is_error = False
        self.failure = None


@pytest.fixture
def mcp_session(monkeypatch):
    """The real `dispatch`, with the MCP client replaced so no connection opens."""
    session = Session()

    class RecordingClient:
        def __init__(self, transport):
            self.transport = transport

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def call_tool(self, name, arguments):
            session.calls.append((name, dict(arguments)))
            if session.failure is not None:
                raise session.failure
            return Result(session.result_is_error)

    monkeypatch.setattr(sentience_mcp, "Client", RecordingClient)
    return session


# --- The tool call carries the thread (acceptance: configured routing) ----------------------


def test_dispatch_sends_message_and_thread_id_in_one_ask_sentience_call(
    monkeypatch, mcp_session
):
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    sentience_mcp.dispatch(ACCESS_TOKEN, "a conflict message")
    assert mcp_session.calls == [
        ("ask_sentience", {"message": "a conflict message", "thread_id": THREAD_ID})
    ]


def test_every_dispatch_uses_the_configured_thread(monkeypatch, mcp_session):
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    for message in ("first", "second", "third"):
        sentience_mcp.dispatch(ACCESS_TOKEN, message)
    assert [arguments["thread_id"] for _, arguments in mcp_session.calls] == [
        THREAD_ID
    ] * 3
    assert [arguments["message"] for _, arguments in mcp_session.calls] == [
        "first",
        "second",
        "third",
    ]


def test_the_thread_is_read_when_dispatching_not_fixed_at_import(
    monkeypatch, mcp_session
):
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    monkeypatch.setenv("SENTIENCE_THREAD_ID", "thread-example-a")
    sentience_mcp.dispatch(ACCESS_TOKEN, "one")
    monkeypatch.setenv("SENTIENCE_THREAD_ID", "thread-example-b")
    sentience_mcp.dispatch(ACCESS_TOKEN, "two")
    assert [arguments["thread_id"] for _, arguments in mcp_session.calls] == [
        "thread-example-a",
        "thread-example-b",
    ]


def test_the_thread_may_come_from_the_dot_env_file(monkeypatch, mcp_session, tmp_path):
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    (tmp_path / ".env").write_text(f"SENTIENCE_THREAD_ID={THREAD_ID}\n")
    sentience_mcp.dispatch(ACCESS_TOKEN, "a conflict message")
    assert mcp_session.calls[0][1]["thread_id"] == THREAD_ID


def test_the_only_tool_called_is_ask_sentience(monkeypatch, mcp_session):
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    sentience_mcp.dispatch(ACCESS_TOKEN, "a conflict message")
    assert {name for name, _ in mcp_session.calls} == {"ask_sentience"}


# --- Missing or empty configuration (failure path) ------------------------------------------


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_dispatch_without_a_thread_id_fails_and_opens_no_session(
    monkeypatch, mcp_session, value
):
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    if value is not None:
        monkeypatch.setenv("SENTIENCE_THREAD_ID", value)
    with pytest.raises(OperatorError) as raised:
        sentience_mcp.dispatch(ACCESS_TOKEN, "a conflict message")
    assert "SENTIENCE_THREAD_ID" in str(raised.value)
    assert ACCESS_TOKEN not in str(raised.value)
    assert mcp_session.calls == []


def test_an_empty_thread_id_in_the_dot_env_file_fails_too(
    monkeypatch, mcp_session, tmp_path
):
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    (tmp_path / ".env").write_text("SENTIENCE_THREAD_ID=\n")
    with pytest.raises(OperatorError):
        sentience_mcp.dispatch(ACCESS_TOKEN, "a conflict message")
    assert mcp_session.calls == []


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_a_conflict_without_a_thread_id_is_not_recorded_and_the_webhook_answers_503(
    client, google, calendars, mcp_session, monkeypatch, value
):
    if value is not None:
        monkeypatch.setenv("SENTIENCE_THREAD_ID", value)
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    assert notify(client).status_code == 503
    assert mcp_session.calls == []
    assert processed() == {}
    assert token_of(WORK) == START_TOKEN
    assert google.writes == []


def test_a_conflict_held_back_for_a_missing_thread_is_dispatched_once_it_is_set(
    client, google, calendars, mcp_session, monkeypatch
):
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    assert notify(client).status_code == 503
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    assert notify(client).status_code == 200
    assert len(mcp_session.calls) == 1
    name, arguments = mcp_session.calls[0]
    assert (name, arguments["thread_id"]) == ("ask_sentience", THREAD_ID)
    assert len(processed()) == 1
    assert token_of(WORK) == NEXT_TOKEN


# --- Configured routing through the webhook, with delivery rules unchanged ------------------


def test_a_conflict_through_the_webhook_is_dispatched_to_the_configured_thread(
    client, google, calendars, mcp_session, monkeypatch
):
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    assert notify(client).status_code == 200
    assert len(mcp_session.calls) == 1
    name, arguments = mcp_session.calls[0]
    assert name == "ask_sentience"
    assert set(arguments) == {"message", "thread_id"}
    assert arguments["thread_id"] == THREAD_ID
    # The thread id is routing, not content: the message is still the conflict message.
    assert payload_of(arguments["message"])["incoming"]["event_id"] == "changed"
    assert THREAD_ID not in arguments["message"]
    assert len(processed()) == 1
    assert token_of(WORK) == NEXT_TOKEN
    assert google.writes == []


def test_two_conflicts_are_two_calls_each_with_the_thread(
    client, google, calendars, mcp_session, monkeypatch
):
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("first", "10:15", "10:45"), meeting("second", "10:30", "11:30")],
    )
    assert notify(client).status_code == 200
    assert len(mcp_session.calls) == 2
    assert {arguments["thread_id"] for _, arguments in mcp_session.calls} == {THREAD_ID}
    assert len(processed()) == 2


def test_a_delivered_conflict_is_not_dispatched_again(
    client, google, calendars, mcp_session, monkeypatch
):
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    changed = meeting("changed", "10:00", "11:00")
    world(google, [changed], [meeting("other", "10:30", "11:30")])
    assert notify(client).status_code == 200
    from test_gcal_webhook import Read

    google.script[(WORK, NEXT_TOKEN)] = Read([[changed]], "work-token-2")
    assert notify(client).status_code == 200
    assert len(mcp_session.calls) == 1


# --- Sentience rejecting the thread (an error result or a failure) --------------------------


def test_an_error_result_such_as_a_rejected_thread_is_a_failed_dispatch(
    monkeypatch, mcp_session
):
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    mcp_session.result_is_error = True
    with pytest.raises(sentience_mcp.DispatchFailed) as raised:
        sentience_mcp.dispatch(ACCESS_TOKEN, "a conflict message")
    assert THREAD_ID not in str(raised.value)
    assert ACCESS_TOKEN not in str(raised.value)


def test_a_rejected_thread_leaves_the_conflict_unrecorded_with_a_503(
    client, google, calendars, mcp_session, monkeypatch
):
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    mcp_session.result_is_error = True
    world(
        google,
        [meeting("changed", "10:00", "11:00")],
        [meeting("other", "10:30", "11:30")],
    )
    assert notify(client).status_code == 503
    assert len(mcp_session.calls) == 1
    assert processed() == {}
    assert token_of(WORK) == START_TOKEN


def test_a_failure_message_never_carries_the_thread_or_the_token(
    monkeypatch, mcp_session
):
    monkeypatch.setenv("SENTIENCE_MCP_URL", "https://mcp.example.com/mcp")
    monkeypatch.setenv("SENTIENCE_THREAD_ID", THREAD_ID)
    mcp_session.failure = RuntimeError(f"bad thread {THREAD_ID} for {ACCESS_TOKEN}")
    with pytest.raises(sentience_mcp.DispatchFailed) as raised:
        sentience_mcp.dispatch(ACCESS_TOKEN, "a conflict message")
    assert THREAD_ID not in str(raised.value)
    assert ACCESS_TOKEN not in str(raised.value)


# --- The setting is declared where the owner provisions it ----------------------------------


def test_the_thread_setting_is_declared_in_env_example_with_no_default():
    assert "SENTIENCE_THREAD_ID" in settings.NAMES
    lines = (ROOT / ".env.example").read_text().splitlines()
    assert "SENTIENCE_THREAD_ID=" in [line.strip() for line in lines]


def test_the_app_stores_no_thread_id():
    # Dispatch Tool Call: the dispatcher stores no thread id; the setting is the only source.
    from test_calendar_attach import execute

    tables = [
        r[0] for r in execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    for table in tables:
        columns = [r[1] for r in execute(f"PRAGMA table_info({table})")]
        assert not [c for c in columns if "thread" in c.lower()], table
