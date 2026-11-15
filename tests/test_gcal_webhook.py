"""Issue #5: `POST /webhooks/gcal` reads the delta of the calendar whose channel notified.

Google is replaced by the `httplib2`-compatible transport the attach tests use, extended with
scripted `events.list` answers keyed by the `syncToken` the request carries. Sentience is
replaced by an `httpx2` transport that must never be called. The database is the libSQL
container.
"""

import threading
import time

import httplib2
import pytest
from fastapi.testclient import TestClient

from dispatcher import google_calendar
from dispatcher.app import app
from test_calendar_attach import (  # noqa: F401 (fixtures)
    HOME,
    WEBHOOK,
    WORK,
    FakeGoogle,
    database,
    google_error,
    rows,
    sentience,
    settings_env,
)
from test_calendar_attach import (
    seed as seed_calendar,
)

PATH = "/webhooks/gcal"
HEADER = "X-Goog-Channel-ID"
WORK_CHANNEL = "channel-work-example"
HOME_CHANNEL = "channel-home-example"
SIDE = "side-project@example.com"
SIDE_CHANNEL = "channel-side-example"
EVENTS_PATH = "/calendar/v3/calendars/{}/events"


def item(n):
    return {"kind": "calendar#event", "id": f"event-{n}", "summary": f"Fictional {n}"}


class Read:
    """A scripted answer to `events.list`: pages of items and the token the last page carries."""

    def __init__(self, pages, next_token):
        self.pages = pages
        self.next_token = next_token


class DeltaGoogle(FakeGoogle):
    """Answers `events.list` from `script`, keyed by (calendar ID, the request's `syncToken`).

    A script value is a `Read`, an HTTP status to fail with, or an exception to raise.
    """

    def __init__(self):
        super().__init__()
        self.script = {}
        self.before_read = None

    def route(self, r):
        if r["method"] == "GET" and r["path"].endswith("/events"):
            calendar_id = r["path"].split("/")[4]
            if self.before_read:
                self.before_read(calendar_id, r["query"])
            answer = self.script.get((calendar_id, r["query"].get("syncToken")))
            if answer is None:
                return google_error(400)
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, int):
                return google_error(answer)
            index = int(r["query"].get("pageToken", "page-0").split("-")[-1])
            payload = {"kind": "calendar#events", "items": answer.pages[index]}
            if index + 1 < len(answer.pages):
                payload["nextPageToken"] = f"page-{index + 1}"
            else:
                payload["nextSyncToken"] = answer.next_token
            return 200, payload
        return super().route(r)


@pytest.fixture
def google(monkeypatch):
    fake = DeltaGoogle()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def calendars():
    """Two active calendars with cursors, and a detached one."""
    seed_calendar(WORK, 1, WORK_CHANNEL, "work-token-0")
    seed_calendar(HOME, 1, HOME_CHANNEL, "home-token-0")
    seed_calendar(SIDE, 0, SIDE_CHANNEL, "side-token-0")


def notify(client, channel=WORK_CHANNEL):
    return client.post(PATH, headers={HEADER: channel})


def token_of(calendar_id):
    return rows()[calendar_id][4]


def only_event_reads(google):
    """Every Google request of the app was an `events.list` read; there was no other call."""
    assert google.writes == []
    assert {(r["method"], r["path"]) for r in google.api_requests} <= {
        ("GET", EVENTS_PATH.format(c)) for c in (WORK, HOME, SIDE)
    }


# --- 1. A notification reads the delta ----------------------------------------------------


def test_the_webhook_path_is_the_address_channels_are_opened_with():
    assert WEBHOOK.endswith(PATH)


def test_a_delta_is_read_with_the_stored_token_and_the_new_token_is_stored(
    client, google, calendars
):
    google.script[(WORK, "work-token-0")] = Read([[item(1), item(2)]], "work-token-1")
    response = notify(client)
    assert response.status_code == 200
    assert len(google.event_reads) == 1
    read = google.event_reads[0]
    assert read["path"] == EVENTS_PATH.format(WORK)
    assert read["query"]["syncToken"] == "work-token-0"
    assert token_of(WORK) == "work-token-1"


def test_the_delta_is_not_expanded_into_single_events(client, google, calendars):
    google.script[(WORK, "work-token-0")] = Read([[item(1)], [item(2)]], "work-token-1")
    notify(client)
    assert google.event_reads
    for read in google.event_reads:
        assert "singleEvents" not in read["query"]


def test_every_page_is_read_and_the_last_pages_token_is_the_one_stored(
    client, google, calendars
):
    google.script[(WORK, "work-token-0")] = Read(
        [[item(1)], [item(2)], [item(3)]], "work-token-last"
    )
    assert notify(client).status_code == 200
    reads = google.event_reads
    assert [r["query"].get("pageToken") for r in reads] == [None, "page-1", "page-2"]
    assert {r["query"].get("syncToken") for r in reads} == {"work-token-0"}
    assert token_of(WORK) == "work-token-last"


def test_a_notification_touches_only_the_calendar_that_owns_the_channel(
    client, google, calendars
):
    google.script[(HOME, "home-token-0")] = Read([[]], "home-token-1")
    assert notify(client, HOME_CHANNEL).status_code == 200
    assert {calendar_of_read(r) for r in google.event_reads} == {HOME}
    assert token_of(HOME) == "home-token-1"
    assert token_of(WORK) == "work-token-0"
    assert token_of(SIDE) == "side-token-0"


def calendar_of_read(request):
    return request["path"].split("/")[4]


def test_an_empty_delta_still_advances_the_cursor(client, google, calendars):
    google.script[(WORK, "work-token-0")] = Read([[]], "work-token-1")
    assert notify(client).status_code == 200
    assert token_of(WORK) == "work-token-1"


def test_successive_notifications_continue_from_the_token_the_last_one_stored(
    client, google, calendars
):
    google.script[(WORK, "work-token-0")] = Read([[item(1)]], "work-token-1")
    google.script[(WORK, "work-token-1")] = Read([[item(2)]], "work-token-2")
    assert notify(client).status_code == 200
    assert notify(client).status_code == 200
    assert [r["query"]["syncToken"] for r in google.event_reads] == [
        "work-token-0",
        "work-token-1",
    ]
    assert token_of(WORK) == "work-token-2"


def test_a_calendar_without_a_cursor_gets_a_full_read_and_a_stored_token(
    client, google
):
    seed_calendar(WORK, 1, WORK_CHANNEL, None)
    google.script[(WORK, None)] = Read([[item(1)], [item(2)]], "work-token-fresh")
    assert notify(client).status_code == 200
    assert all("syncToken" not in r["query"] for r in google.event_reads)
    assert token_of(WORK) == "work-token-fresh"


def test_the_notification_answers_nothing_to_sentience(
    client,
    google,
    calendars,
    sentience,  # noqa: F811
):
    google.script[(WORK, "work-token-0")] = Read([[item(1)]], "work-token-1")
    assert notify(client).status_code == 200
    assert sentience == []


# --- 2. Unknown channels and a missing header ---------------------------------------------


def test_an_unknown_channel_answers_404_and_makes_no_google_call(
    client, google, calendars
):
    assert notify(client, "channel-nobody-example").status_code == 404
    assert google.requests == []
    assert token_of(WORK) == "work-token-0"


def test_a_detached_calendars_channel_answers_404_and_makes_no_google_call(
    client, google, calendars
):
    assert notify(client, SIDE_CHANNEL).status_code == 404
    assert google.requests == []
    assert token_of(SIDE) == "side-token-0"


def test_no_calendar_at_all_answers_404(client, google):
    assert notify(client).status_code == 404
    assert google.requests == []


def test_a_missing_channel_header_answers_400_and_makes_no_google_call(
    client, google, calendars
):
    assert client.post(PATH).status_code == 400
    assert google.requests == []
    assert {c: token_of(c) for c in (WORK, HOME)} == {
        WORK: "work-token-0",
        HOME: "home-token-0",
    }


def test_other_google_headers_do_not_stand_in_for_the_channel_id(
    client, google, calendars
):
    headers = {"X-Goog-Resource-State": "exists", "X-Goog-Resource-ID": "resource-1"}
    assert client.post(PATH, headers=headers).status_code == 400
    assert google.requests == []


def test_a_channel_id_is_matched_exactly(client, google, calendars):
    for channel in (WORK_CHANNEL.upper(), WORK_CHANNEL + "x", WORK_CHANNEL[:-1]):
        assert notify(client, channel).status_code == 404
    assert google.requests == []


# --- 3. 410 Gone --------------------------------------------------------------------------


def test_a_410_clears_the_token_and_a_full_read_stores_a_fresh_one(
    client, google, calendars
):
    google.script[(WORK, "work-token-0")] = 410
    google.script[(WORK, None)] = Read([[item(1)], [item(2)], [item(3)]], "work-fresh")
    assert notify(client).status_code == 200
    queries = [r["query"] for r in google.event_reads]
    assert queries[0]["syncToken"] == "work-token-0"
    full = queries[1:]
    assert [q.get("pageToken") for q in full] == [None, "page-1", "page-2"]
    assert all("syncToken" not in q for q in full)
    assert all("singleEvents" not in q for q in queries)
    assert token_of(WORK) == "work-fresh"


def test_the_stale_token_is_not_kept_after_a_410(client, google, calendars):
    google.script[(WORK, "work-token-0")] = 410
    google.script[(WORK, None)] = Read([[]], "work-fresh")
    notify(client)
    assert token_of(WORK) != "work-token-0"


def test_delta_monitoring_resumes_from_the_fresh_token_after_a_410(
    client, google, calendars
):
    google.script[(WORK, "work-token-0")] = 410
    google.script[(WORK, None)] = Read([[]], "work-fresh")
    google.script[(WORK, "work-fresh")] = Read([[item(1)]], "work-next")
    assert notify(client).status_code == 200
    assert notify(client).status_code == 200
    assert google.event_reads[-1]["query"]["syncToken"] == "work-fresh"
    assert token_of(WORK) == "work-next"


def test_a_410_on_one_calendar_leaves_the_others_alone(client, google, calendars):
    google.script[(WORK, "work-token-0")] = 410
    google.script[(WORK, None)] = Read([[]], "work-fresh")
    notify(client)
    assert token_of(HOME) == "home-token-0"
    assert {calendar_of_read(r) for r in google.event_reads} == {WORK}


def test_a_failing_full_read_after_a_410_answers_503_and_a_later_notification_recovers(
    client, google, calendars
):
    google.script[(WORK, "work-token-0")] = 410
    google.script[(WORK, None)] = 500
    assert notify(client).status_code == 503
    google.script[(WORK, None)] = Read([[item(1)]], "work-fresh")
    google.script[(WORK, "work-token-0")] = 410
    assert notify(client).status_code == 200
    assert token_of(WORK) == "work-fresh"


def test_a_410_from_a_full_read_is_an_ordinary_failure(client, google, calendars):
    google.script[(WORK, "work-token-0")] = 410
    google.script[(WORK, None)] = 410
    assert notify(client).status_code == 503


# --- 4. Any other failure answers 503 and leaves the cursor ------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_a_google_error_answers_503_and_leaves_the_token(
    client, google, calendars, status
):
    google.script[(WORK, "work-token-0")] = status
    assert notify(client).status_code == 503
    assert token_of(WORK) == "work-token-0"


@pytest.mark.parametrize(
    "failure",
    [OSError("connection reset"), httplib2.ServerNotFoundError("no such host")],
    ids=["os-error", "server-not-found"],
)
def test_a_transport_failure_answers_503_and_leaves_the_token(
    client, google, calendars, failure
):
    google.script[(WORK, "work-token-0")] = failure
    assert notify(client).status_code == 503
    assert token_of(WORK) == "work-token-0"


def test_a_failure_on_a_later_page_stores_no_token(client, google, calendars):
    google.script[(WORK, "work-token-0")] = Read([[item(1)], [item(2)]], "work-token-1")
    original = google.route

    def fail_second_page(r):
        if r["query"].get("pageToken") == "page-1" and r["path"].endswith("/events"):
            return google_error(500)
        return original(r)

    google.route = fail_second_page
    assert notify(client).status_code == 503
    assert token_of(WORK) == "work-token-0"


def test_a_failed_notification_can_be_retried_and_then_succeeds(
    client, google, calendars
):
    google.script[(WORK, "work-token-0")] = 500
    assert notify(client).status_code == 503
    google.script[(WORK, "work-token-0")] = Read([[item(1)]], "work-token-1")
    assert notify(client).status_code == 200
    assert token_of(WORK) == "work-token-1"


def test_a_failure_on_one_calendar_does_not_disturb_another(client, google, calendars):
    google.script[(WORK, "work-token-0")] = 500
    google.script[(HOME, "home-token-0")] = Read([[]], "home-token-1")
    assert notify(client).status_code == 503
    assert notify(client, HOME_CHANNEL).status_code == 200
    assert token_of(WORK) == "work-token-0"
    assert token_of(HOME) == "home-token-1"


# --- 5. One delta at a time ---------------------------------------------------------------


class Gate:
    """Holds the first `events.list` until released and records when each read starts."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.reads = []
        self._lock = threading.Lock()

    def __call__(self, calendar_id, query):
        with self._lock:
            self.reads.append((calendar_id, query.get("syncToken")))
            first = len(self.reads) == 1
        if first:
            self.entered.set()
            assert self.release.wait(timeout=60)


def notify_in_thread(channel, results):
    def post():
        with TestClient(app) as thread_client:
            results.append(notify(thread_client, channel).status_code)

    thread = threading.Thread(target=post)
    thread.start()
    return thread


@pytest.mark.parametrize(
    ("second_channel", "second_calendar"),
    [(WORK_CHANNEL, WORK), (HOME_CHANNEL, HOME)],
    ids=["same-calendar", "other-calendar"],
)
def test_a_notification_waits_for_the_delta_being_processed(
    google, calendars, second_channel, second_calendar
):
    gate = Gate()
    google.before_read = gate
    google.script[(WORK, "work-token-0")] = Read([[item(1)]], "work-token-1")
    google.script[(HOME, "home-token-0")] = Read([[item(2)]], "home-token-1")
    google.script[(WORK, "work-token-1")] = Read([[item(3)]], "work-token-2")
    results = []
    first = notify_in_thread(WORK_CHANNEL, results)
    try:
        assert gate.entered.wait(timeout=60)
        second = notify_in_thread(second_channel, results)
        time.sleep(1.5)
        assert len(gate.reads) == 1, (
            "the second delta started before the first finished"
        )
        assert token_of(WORK) == "work-token-0"
    finally:
        gate.release.set()
    first.join(timeout=60)
    second.join(timeout=60)
    assert results == [200, 200]
    assert len(gate.reads) == 2
    # The second delta ran after the first had stored its cursor.
    expected = "work-token-1" if second_calendar == WORK else "home-token-0"
    assert gate.reads[1] == (second_calendar, expected)
    assert token_of(WORK) == (
        "work-token-2" if second_calendar == WORK else "work-token-1"
    )


def test_a_failed_delta_releases_the_wait(google, calendars):
    gate = Gate()
    google.before_read = gate
    google.script[(WORK, "work-token-0")] = 500
    google.script[(HOME, "home-token-0")] = Read([[]], "home-token-1")
    results = []
    first = notify_in_thread(WORK_CHANNEL, results)
    try:
        assert gate.entered.wait(timeout=60)
        second = notify_in_thread(HOME_CHANNEL, results)
        time.sleep(0.5)
    finally:
        gate.release.set()
    first.join(timeout=60)
    second.join(timeout=60)
    assert sorted(results) == [200, 503]
    assert token_of(HOME) == "home-token-1"


# --- 6. No Google write -------------------------------------------------------------------


def test_a_delta_makes_no_google_write(client, google, calendars):
    google.script[(WORK, "work-token-0")] = Read([[item(1)], [item(2)]], "work-token-1")
    assert notify(client).status_code == 200
    only_event_reads(google)


def test_a_410_recovery_makes_no_google_write(client, google, calendars):
    google.script[(WORK, "work-token-0")] = 410
    google.script[(WORK, None)] = Read([[item(1)]], "work-fresh")
    assert notify(client).status_code == 200
    only_event_reads(google)


def test_a_failed_notification_makes_no_google_write(client, google, calendars):
    google.script[(WORK, "work-token-0")] = 500
    assert notify(client).status_code == 503
    only_event_reads(google)


def test_a_notification_changes_no_calendar_row_but_its_token(
    client, google, calendars
):
    before = rows()
    google.script[(WORK, "work-token-0")] = Read([[item(1)]], "work-token-1")
    notify(client)
    after = rows()
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {WORK}
    assert after[WORK][:4] == before[WORK][:4]
    assert after[WORK][5:] == before[WORK][5:]
