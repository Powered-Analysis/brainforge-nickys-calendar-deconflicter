"""Issue #3: `POST /internal/renew-watches` renews every active channel, and
`calendar detach <calendar_id>` closes one.

Google is replaced by the `httplib2`-compatible transport the attach tests use, extended with
`channels.stop`. Sentience is replaced by an `httpx2` transport that must never be called. The
database is the libSQL container.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT
from dispatcher import cli, google_calendar
from dispatcher.app import app
from test_calendar_attach import (  # noqa: F401 (fixtures)
    ACCESS_TOKEN,
    COLUMNS,
    EXPIRATION_MS,
    HOME,
    SIDE,
    WEBHOOK,
    WORK,
    FakeGoogle,
    calendar_of,
    database,
    execute,
    google_error,
    rows,
    sentience,
    settings_env,
)

SECRET = "renewal-secret-example-value"
URL = "/internal/renew-watches"
AUTH = {"Authorization": f"Bearer {SECRET}"}
STOP_PATH = "/calendar/v3/channels/stop"


class FakeGoogleWithStop(FakeGoogle):
    """Adds `channels.stop`, which answers 204 unless the channel ID has a failure queued."""

    def __init__(self):
        super().__init__()
        self.stop_failures = {}

    def route(self, r):
        if r["path"] == STOP_PATH and r["method"] == "POST":
            channel = json.loads(r["body"])
            if channel["id"] in self.stop_failures:
                return google_error(self.stop_failures[channel["id"]])
            return 204, {}
        return super().route(r)

    @property
    def stops(self):
        return [r for r in self.api_requests if r["path"] == STOP_PATH]

    def stop_bodies(self):
        return [json.loads(r["body"]) for r in self.stops]

    @property
    def other_writes(self):
        return [
            r
            for r in self.writes
            if r["path"] != STOP_PATH and not r["path"].endswith("/events/watch")
        ]


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogleWithStop()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_CRON_SECRET", SECRET)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def seed(calendar_id, active=1, n=1, sync_token="old-sync-token"):
    """A calendar row with a channel and resource unique to it."""
    execute(
        "INSERT INTO calendars (calendar_id, channel_id, resource_id, channel_expiration,"
        " sync_token, active, created_at) VALUES (?, ?, ?, 4242, ?, ?, 1700000000)",
        (calendar_id, f"old-channel-{n}", f"old-resource-{n}", sync_token, active),
    )


@pytest.fixture
def three(google):
    """Two active calendars and one inactive."""
    seed(WORK, 1, 1)
    seed(HOME, 1, 2)
    seed(SIDE, 0, 3)
    return rows()


@pytest.fixture
def run(capsys):
    """Runs `calendar detach` with the given arguments; returns (exit status, stdout, stderr)."""

    def invoke(*arguments):
        capsys.readouterr()
        try:
            cli.main(["calendar", "detach", *arguments])
            status = 0
        except SystemExit as exit_:
            status = exit_.code if exit_.code is not None else 0
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    return invoke


# --- 1. Renewal opens a new channel, stores it, then stops the old one --------------------


def test_each_active_calendar_gets_a_new_channel_of_the_same_type_and_address(
    client, google, three
):
    assert client.post(URL, headers=AUTH).status_code == 200
    bodies = {calendar_of(r): json.loads(r["body"]) for r in google.watches}
    assert set(bodies) == {WORK, HOME}
    for body in bodies.values():
        assert body["type"] == "web_hook"
        assert body["address"] == WEBHOOK
        assert str(uuid.UUID(body["id"])) == body["id"]
    assert len({b["id"] for b in bodies.values()}) == 2
    assert not {b["id"] for b in bodies.values()} & {"old-channel-1", "old-channel-2"}


def test_the_new_channel_is_stored_and_nothing_else_about_the_calendar_changes(
    client, google, three
):
    client.post(URL, headers=AUTH)
    after = rows()
    new_ids = {calendar_of(r): json.loads(r["body"])["id"] for r in google.watches}
    for calendar_id in (WORK, HOME):
        before_row, after_row = three[calendar_id], after[calendar_id]
        assert after_row[1] == new_ids[calendar_id]
        assert after_row[2] == f"resource-for-{calendar_id}"
        assert after_row[3] == EXPIRATION_MS
        # sync_token, active, and created_at keep their values.
        assert after_row[4:] == before_row[4:]
        assert after_row[0] == calendar_id


def test_the_old_channel_is_stopped_with_its_channel_and_resource_ids(
    client, google, three
):
    client.post(URL, headers=AUTH)
    assert sorted(google.stop_bodies(), key=lambda b: b["id"]) == [
        {"id": "old-channel-1", "resourceId": "old-resource-1"},
        {"id": "old-channel-2", "resourceId": "old-resource-2"},
    ]


def test_each_calendar_is_watched_before_its_old_channel_is_stopped(
    client, google, three
):
    client.post(URL, headers=AUTH)
    order = [r for r in google.api_requests if r in google.watches + google.stops]
    seen_watch = set()
    for request in order:
        if request in google.watches:
            seen_watch.add(calendar_of(request))
        else:
            old = json.loads(request["body"])["id"]
            owner = WORK if old == "old-channel-1" else HOME
            assert owner in seen_watch


def test_an_inactive_calendar_is_untouched(client, google, three):
    client.post(URL, headers=AUTH)
    assert rows()[SIDE] == three[SIDE]
    assert SIDE not in {calendar_of(r) for r in google.watches}
    assert "old-channel-3" not in {b["id"] for b in google.stop_bodies()}


def test_renewing_twice_stops_the_channel_the_first_renewal_opened(
    client, google, three
):
    client.post(URL, headers=AUTH)
    first = {calendar_of(r): json.loads(r["body"])["id"] for r in google.watches}
    google.requests.clear()
    assert client.post(URL, headers=AUTH).status_code == 200
    second = {calendar_of(r): json.loads(r["body"])["id"] for r in google.watches}
    assert all(first[c] != second[c] for c in first)
    assert {b["id"] for b in google.stop_bodies()} == set(first.values())
    assert {rows()[c][1] for c in first} == set(second.values())


# --- 2. Success answers 200 ---------------------------------------------------------------


def test_success_answers_200_with_an_empty_body(client, google, three):
    response = client.post(URL, headers=AUTH)
    assert response.status_code == 200
    assert response.content == b""


def test_no_active_calendars_answers_200_and_calls_nothing(client, google):
    response = client.post(URL, headers=AUTH)
    assert response.status_code == 200
    assert google.requests == []


def test_only_inactive_calendars_answers_200_and_calls_nothing(client, google):
    seed(SIDE, 0, 3)
    assert client.post(URL, headers=AUTH).status_code == 200
    assert google.requests == []


# --- 3. Failures: the rest still renew, then 502 ------------------------------------------


@pytest.mark.parametrize("failing", [WORK, HOME], ids=["first", "last"])
def test_a_calendar_whose_watch_fails_keeps_its_old_record_and_the_rest_renew(
    client, google, three, failing
):
    google.watch_failures[failing] = 500
    other = HOME if failing == WORK else WORK
    response = client.post(URL, headers=AUTH)
    assert response.status_code == 502
    after = rows()
    assert after[failing] == three[failing]
    assert after[other][1] != three[other][1]
    opened = {calendar_of(r): json.loads(r["body"])["id"] for r in google.watches}
    assert after[other][1] == opened[other]
    assert after[other][2] == f"resource-for-{other}"


def test_a_failed_watch_does_not_stop_the_old_channel(client, google, three):
    google.watch_failures[WORK] = 500
    client.post(URL, headers=AUTH)
    assert "old-channel-1" not in {b["id"] for b in google.stop_bodies()}
    assert "old-channel-2" in {b["id"] for b in google.stop_bodies()}


def test_every_watch_failing_answers_502_and_changes_no_record(client, google, three):
    google.watch_failures[WORK] = 403
    google.watch_failures[HOME] = 500
    assert client.post(URL, headers=AUTH).status_code == 502
    assert rows() == three
    assert google.stops == []


def test_a_stop_google_answers_with_404_is_not_a_failure(client, google, three):
    google.stop_failures["old-channel-1"] = 404
    google.stop_failures["old-channel-2"] = 404
    assert client.post(URL, headers=AUTH).status_code == 200
    assert rows()[WORK][1] != "old-channel-1"


@pytest.mark.parametrize("status", [400, 403, 410, 500])
def test_any_other_failed_stop_answers_502_with_the_new_channel_still_recorded(
    client, google, three, status
):
    google.stop_failures["old-channel-1"] = status
    assert client.post(URL, headers=AUTH).status_code == 502
    after = rows()
    new_ids = {calendar_of(r): json.loads(r["body"])["id"] for r in google.watches}
    assert after[WORK][1] == new_ids[WORK]
    assert after[WORK][2] == f"resource-for-{WORK}"
    assert after[HOME][1] == new_ids[HOME]


def test_a_failed_stop_on_one_calendar_does_not_stop_the_others_renewing(
    client, google, three
):
    google.stop_failures["old-channel-1"] = 500
    client.post(URL, headers=AUTH)
    assert {calendar_of(r) for r in google.watches} == {WORK, HOME}
    assert "old-channel-2" in {b["id"] for b in google.stop_bodies()}


def test_a_later_ping_succeeds_once_google_recovers(client, google, three):
    google.watch_failures[WORK] = 500
    assert client.post(URL, headers=AUTH).status_code == 502
    del google.watch_failures[WORK]
    assert client.post(URL, headers=AUTH).status_code == 200
    assert rows()[WORK][1] not in {"old-channel-1"}


# --- 4. Authentication runs before any Google call ----------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {SECRET}"},
        {"Authorization": "Bearer "},
    ],
    ids=["none", "wrong-token", "other-scheme", "empty-token"],
)
def test_a_request_that_fails_authentication_makes_no_google_call(
    client, google, three, headers
):
    assert client.post(URL, headers=headers).status_code == 401
    assert google.requests == []
    assert rows() == three


def test_with_no_secret_configured_nothing_is_renewed(
    client, google, three, monkeypatch
):
    monkeypatch.delenv("INTERNAL_CRON_SECRET")
    assert client.post(URL, headers=AUTH).status_code == 500
    assert google.requests == []
    assert rows() == three


# --- 5. The response body names nothing ---------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    ["watch", "stop"],
)
def test_a_failure_response_names_no_calendar_channel_or_resource(
    client, google, three, failure
):
    if failure == "watch":
        google.watch_failures[WORK] = 500
    else:
        google.stop_failures["old-channel-1"] = 500
    response = client.post(URL, headers=AUTH)
    assert response.status_code == 502
    names = [WORK, HOME, SIDE, "old-channel", "old-resource", "resource-for-"]
    names += [json.loads(r["body"])["id"] for r in google.watches]
    for name in names:
        assert name not in response.text
    assert ACCESS_TOKEN not in response.text


def test_a_failure_message_is_not_logged_with_a_calendar_name(
    client, google, three, caplog
):
    google.watch_failures[WORK] = 500
    client.post(URL, headers=AUTH)
    assert WORK not in caplog.text
    assert HOME not in caplog.text


# --- 9. Writes to Google are only events.watch and channels.stop --------------------------


def test_renewal_writes_to_google_only_with_watch_and_stop(client, google, three):
    client.post(URL, headers=AUTH)
    assert google.other_writes == []
    assert {r["method"] for r in google.api_requests} == {"POST"}
    assert len(google.watches) == 2
    assert len(google.stops) == 2


def test_renewal_never_talks_to_sentience(client, google, three, sentience):  # noqa: F811
    client.post(URL, headers=AUTH)
    assert sentience == []


# --- 11. No database setting: 500, and an unauthenticated request still gets 401 ----------


def test_without_a_database_setting_an_authenticated_ping_renews_nothing(
    google, monkeypatch
):
    with monkeypatch.context() as scoped:
        scoped.delenv("TURSO_DATABASE_URL")
        with TestClient(app) as test_client:
            assert test_client.post(URL, headers=AUTH).status_code == 500
    assert google.requests == []


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}], ids=["none", "wrong-token"]
)
def test_without_a_database_setting_an_unauthenticated_ping_still_gets_401(
    google, monkeypatch, headers
):
    with monkeypatch.context() as scoped:
        scoped.delenv("TURSO_DATABASE_URL")
        with TestClient(app) as test_client:
            response = test_client.post(URL, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert google.requests == []


# --- 6. Detach on an active calendar ------------------------------------------------------


def test_detach_stops_the_channel_then_marks_the_calendar_inactive(google, three, run):
    status, _, _ = run(WORK)
    assert status == 0
    assert google.stop_bodies() == [
        {"id": "old-channel-1", "resourceId": "old-resource-1"}
    ]
    after = rows()
    assert after[WORK][5] == 0


def test_detach_keeps_the_row_and_changes_only_active(google, three, run):
    run(WORK)
    before = list(three[WORK])
    before[5] = 0
    assert list(rows()[WORK]) == before


def test_detach_leaves_every_other_calendar_alone(google, three, run):
    run(WORK)
    assert rows()[HOME] == three[HOME]
    assert rows()[SIDE] == three[SIDE]


def test_detach_with_a_stop_google_answers_404_still_marks_it_inactive(
    google, three, run
):
    google.stop_failures["old-channel-1"] = 404
    status, _, _ = run(WORK)
    assert status == 0
    assert rows()[WORK][5] == 0


def test_a_detached_calendar_is_left_alone_by_the_next_renewal(
    client, google, three, run
):
    run(WORK)
    google.requests.clear()
    assert client.post(URL, headers=AUTH).status_code == 200
    assert {calendar_of(r) for r in google.watches} == {HOME}
    assert "old-channel-1" not in {b["id"] for b in google.stop_bodies()}


def test_detach_stops_the_channel_the_last_renewal_opened(client, google, three, run):
    client.post(URL, headers=AUTH)
    new_id = rows()[WORK][1]
    google.requests.clear()
    assert run(WORK)[0] == 0
    assert google.stop_bodies() == [
        {"id": new_id, "resourceId": f"resource-for-{WORK}"}
    ]


# --- 7. Detach on a calendar that cannot be detached --------------------------------------


def test_detach_of_a_calendar_with_no_row_fails_and_calls_nothing(google, three, run):
    status, out, err = run("nobody@example.com")
    assert status not in (0, None)
    assert (out + err).strip()
    assert google.requests == []
    assert rows() == three


def test_detach_of_an_inactive_calendar_fails_and_calls_nothing(google, three, run):
    status, out, err = run(SIDE)
    assert status not in (0, None)
    assert (out + err).strip()
    assert google.requests == []
    assert rows()[SIDE] == three[SIDE]


def test_detaching_twice_fails_the_second_time_without_a_google_call(
    google, three, run
):
    assert run(WORK)[0] == 0
    google.requests.clear()
    status, _, _ = run(WORK)
    assert status not in (0, None)
    assert google.requests == []


# --- 8. A failed stop leaves the calendar active ------------------------------------------


@pytest.mark.parametrize("status", [400, 403, 410, 500])
def test_a_failed_stop_leaves_the_calendar_active_and_exits_nonzero(
    google, three, run, status
):
    google.stop_failures["old-channel-1"] = status
    code, out, err = run(WORK)
    assert code not in (0, None)
    assert (out + err).strip()
    assert rows()[WORK] == three[WORK]


def test_detach_can_be_run_again_after_a_failed_stop(google, three, run):
    google.stop_failures["old-channel-1"] = 500
    assert run(WORK)[0] not in (0, None)
    del google.stop_failures["old-channel-1"]
    assert run(WORK)[0] == 0
    assert rows()[WORK][5] == 0


# --- 9. Writes to Google are only events.watch and channels.stop --------------------------


def test_detach_makes_exactly_one_google_write_and_it_is_channels_stop(
    google, three, run
):
    run(WORK)
    assert google.watches == []
    assert google.other_writes == []
    assert len(google.stops) == 1


def test_detach_never_talks_to_sentience(google, three, run, sentience):  # noqa: F811
    run(WORK)
    assert sentience == []


# --- Cross-cutting ------------------------------------------------------------------------


def test_detach_without_a_calendar_id_is_a_usage_error_that_calls_nothing(
    google, three
):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["calendar", "detach"])
    assert exit_.value.code == 2
    assert google.requests == []
    assert rows() == three


def test_every_row_keeps_all_columns_after_renewal_and_detach(
    client, google, three, run
):
    client.post(URL, headers=AUTH)
    run(HOME)
    for calendar_id, row in rows().items():
        assert len(row) == len(COLUMNS.split(", "))
        assert row[0] == calendar_id


# --- 10. README ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "needle",
    [
        "python -m dispatcher calendar detach <calendar_id>",
        "/internal/renew-watches",
        "502",
        "404",
    ],
)
def test_readme_documents_detach_and_what_renewal_does(needle):
    assert needle in (ROOT / "README.md").read_text()
