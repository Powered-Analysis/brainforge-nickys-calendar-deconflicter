"""Issue #2: `calendar attach <calendar_id> | all | --auto-discover`.

Google is replaced by an `httplib2`-compatible transport injected into the one Google Calendar
module, and Sentience by an `httpx2` transport that must never be called. The database is the
libSQL container.
"""

import json
import re
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

import httplib2
import httpx2
import pytest
from googleapiclient import discovery_cache

from conftest import ROOT
from dispatcher import cli, db, google_calendar, http

BASE_URL = "https://dispatcher-example.fly.dev"
WEBHOOK = f"{BASE_URL}/webhooks/gcal"
CLIENT_ID = "client-id-example.apps.example.com"
CLIENT_SECRET = "client-secret-example"
REFRESH_TOKEN = "refresh-token-example"
ACCESS_TOKEN = "access-token-example"

WORK = "work@example.com"
HOME = "home@example.com"
SIDE = "side-project@example.com"
SHARED = "shared-team@example.org"
READONLY = "holidays@example.org"

# Larger than 2**31, so a value narrowed to 32 bits or converted to seconds is caught.
EXPIRATION_MS = 1893456000123

COLUMNS = "calendar_id, channel_id, resource_id, channel_expiration, sync_token, active, created_at"

ALL_FORMS = pytest.mark.parametrize(
    "arguments", [["all"], ["--auto-discover"]], ids=["all", "auto-discover"]
)


def entry(calendar_id, role="owner", summary=None):
    return {
        "kind": "calendar#calendarListEntry",
        "id": calendar_id,
        "summary": summary or f"Fictional {calendar_id}",
        "accessRole": role,
    }


def google_error(status):
    return status, {
        "error": {
            "code": status,
            "message": "fictional failure",
            "errors": [{"reason": "fictional"}],
        }
    }


class FakeGoogle:
    """Stands in for Google's token endpoint and Calendar API and records every request."""

    def __init__(self):
        self.requests = []
        self.timeout = None
        self.entries = {}
        self.aliases = {}
        self.list_pages = None
        self.event_pages = {}
        self.sync_tokens = {}
        self.watch_failures = {}
        self.events_failures = {}
        self.list_failure = None
        self.add(entry(WORK, summary="Fictional Work"))

    def add(self, calendar_entry, alias=None):
        self.entries[calendar_entry["id"]] = calendar_entry
        if alias:
            self.aliases[alias] = calendar_entry["id"]

    # httplib2.Http.request, as the Google client library calls it.
    def request(
        self,
        uri,
        method="GET",
        body=None,
        headers=None,
        redirections=5,
        connection_type=None,
        **kwargs,
    ):
        parts = urlsplit(uri)
        record = {
            "method": method,
            "host": parts.netloc,
            "scheme": parts.scheme,
            "path": unquote(parts.path),
            "query": {k: v[0] for k, v in parse_qs(parts.query).items()},
            "body": body.decode() if isinstance(body, bytes) else body,
            "headers": dict(headers or {}),
        }
        self.requests.append(record)
        status, payload = self.route(record)
        response = httplib2.Response(
            {"status": str(status), "content-type": "application/json"}
        )
        return response, json.dumps(payload).encode()

    def resolve(self, name):
        return self.aliases.get(name, name)

    def route(self, r):
        path, method = r["path"], r["method"]
        if r["host"] == "oauth2.googleapis.com" and path == "/token":
            return 200, {
                "access_token": ACCESS_TOKEN,
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        match = re.fullmatch(r"/calendar/v3/users/me/calendarList/(.+)", path)
        if match and method == "GET":
            found = self.entries.get(self.resolve(match.group(1)))
            return (200, found) if found else google_error(404)
        if path == "/calendar/v3/users/me/calendarList" and method == "GET":
            if self.list_failure:
                return google_error(self.list_failure)
            pages = self.list_pages or [list(self.entries.values())]
            index = int(r["query"].get("pageToken", "list-page-0").split("-")[-1])
            payload = {"kind": "calendar#calendarList", "items": pages[index]}
            if index + 1 < len(pages):
                payload["nextPageToken"] = f"list-page-{index + 1}"
            return 200, payload
        match = re.fullmatch(r"/calendar/v3/calendars/(.+)/events", path)
        if match and method == "GET":
            calendar_id = self.resolve(match.group(1))
            if calendar_id in self.events_failures:
                return google_error(self.events_failures[calendar_id])
            pages = self.event_pages.get(calendar_id, [[]])
            index = int(r["query"].get("pageToken", "events-page-0").split("-")[-1])
            payload = {"kind": "calendar#events", "items": pages[index]}
            if index + 1 < len(pages):
                payload["nextPageToken"] = f"events-page-{index + 1}"
            else:
                payload["nextSyncToken"] = self.sync_tokens.get(
                    calendar_id, f"sync-token-for-{calendar_id}"
                )
            return 200, payload
        match = re.fullmatch(r"/calendar/v3/calendars/(.+)/events/watch", path)
        if match and method == "POST":
            calendar_id = self.resolve(match.group(1))
            if calendar_id in self.watch_failures:
                return google_error(self.watch_failures[calendar_id])
            channel = json.loads(r["body"])
            return 200, {
                "kind": "api#channel",
                "id": channel["id"],
                "resourceId": f"resource-for-{calendar_id}",
                "resourceUri": f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                "expiration": str(EXPIRATION_MS),
            }
        return 404, {"error": {"code": 404, "message": "unexpected request"}}

    @property
    def api_requests(self):
        return [r for r in self.requests if r["path"].startswith("/calendar/v3/")]

    @property
    def watches(self):
        return [r for r in self.api_requests if r["path"].endswith("/events/watch")]

    @property
    def event_reads(self):
        return [
            r
            for r in self.api_requests
            if r["method"] == "GET" and r["path"].endswith("/events")
        ]

    @property
    def token_requests(self):
        return [r for r in self.requests if r["path"] == "/token"]

    @property
    def writes(self):
        return [r for r in self.api_requests if r["method"] not in ("GET", "HEAD")]

    def watch_bodies(self):
        return [json.loads(r["body"]) for r in self.watches]


def calendar_of(request):
    return request["path"].split("/")[4]


def execute(sql, params=()):
    conn = db.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def rows():
    return {r[0]: tuple(r) for r in execute(f"SELECT {COLUMNS} FROM calendars")}


def seed(calendar_id, active, channel_id="old-channel", sync_token="old-sync-token"):
    execute(
        "INSERT INTO calendars (calendar_id, channel_id, resource_id, channel_expiration,"
        " sync_token, active, created_at) VALUES (?, ?, 'old-resource', 4242, ?, ?, 1700000000)",
        (calendar_id, channel_id, sync_token, active),
    )
    return rows()[calendar_id]


def wipe():
    for table in ("calendars", "processed_events"):
        execute(f"DELETE FROM {table}")


@pytest.fixture(autouse=True)
def database():
    db.ensure_schema()
    wipe()
    yield
    wipe()


@pytest.fixture(autouse=True)
def sentience(monkeypatch):
    """Attaching never talks to Sentience, so any request to it is recorded and fails the test."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(500)

    monkeypatch.setattr(http, "transport", httpx2.MockTransport(handler))
    return seen


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    monkeypatch.setenv("GCP_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GCP_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("GCP_REFRESH_TOKEN", REFRESH_TOKEN)
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE_URL)


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(google_calendar, "transport", fake)
    return fake


@pytest.fixture
def run(capsys):
    """Runs `calendar attach` with the given arguments; returns (exit status, stdout, stderr)."""

    def invoke(*arguments):
        capsys.readouterr()
        try:
            cli.main(["calendar", "attach", *arguments])
            status = 0
        except SystemExit as exit_:
            status = exit_.code if exit_.code is not None else 0
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    return invoke


@pytest.fixture
def mixed_list(google):
    """Owned calendars spread over three pages of the calendar list, and some not owned."""
    google.entries.clear()
    google.list_pages = [
        [entry(SHARED, role="writer"), entry(WORK, summary="Fictional Work")],
        [entry(READONLY, role="reader"), entry(HOME, summary="Fictional Home")],
        [entry(SIDE, summary="Fictional Side")],
    ]
    for page in google.list_pages:
        for item in page:
            google.entries[item["id"]] = item
    return google


# --- 1. One module, injected transport ----------------------------------------------------


def test_every_google_request_goes_through_the_injected_transport(google, run):
    status, _, _ = run(WORK)
    assert status == 0
    assert google.requests
    assert {r["scheme"] for r in google.requests} == {"https"}
    assert {r["host"] for r in google.requests} <= {
        "oauth2.googleapis.com",
        "www.googleapis.com",
    }


# --- 2. Credentials and scope -------------------------------------------------------------


def test_the_module_authenticates_as_the_operator_with_the_three_settings(google, run):
    run(WORK)
    form = {k: v[0] for k, v in parse_qs(google.token_requests[0]["body"]).items()}
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == REFRESH_TOKEN
    assert form["client_id"] == CLIENT_ID
    assert form["client_secret"] == CLIENT_SECRET


def test_calendar_requests_carry_the_access_token_google_issued(google, run):
    run(WORK)
    assert google.api_requests
    for request in google.api_requests:
        headers = {k.lower(): v for k, v in request["headers"].items()}
        assert headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_no_scope_beyond_read_only_calendar_access_is_requested(google, run):
    run(WORK)
    for request in google.token_requests:
        form = {k: v[0] for k, v in parse_qs(request["body"]).items()}
        for scope in form.get("scope", "").split():
            assert scope.startswith("https://www.googleapis.com/auth/calendar")
            assert scope.endswith(".readonly"), scope


@pytest.mark.parametrize(
    "method_id",
    [
        "calendar.calendarList.get",
        "calendar.calendarList.list",
        "calendar.events.list",
        "calendar.events.instances",
        "calendar.events.watch",
        "calendar.channels.stop",
    ],
)
def test_calendar_readonly_is_accepted_by_every_call_the_app_makes(method_id):
    document = json.loads(discovery_cache.get_static_doc("calendar", "v3"))
    methods = {}

    def collect(node):
        for method in node.get("methods", {}).values():
            methods[method["id"]] = method
        for child in node.get("resources", {}).values():
            collect(child)

    collect(document)
    scope = "https://www.googleapis.com/auth/calendar.readonly"
    assert scope in methods[method_id]["scopes"]


# --- 3. Attaching one calendar ------------------------------------------------------------


def test_attaching_an_owned_calendar_stores_one_active_row(google, run):
    status, out, _ = run(WORK)
    assert status == 0
    stored = rows()
    assert list(stored) == [WORK]
    calendar_id, channel_id, resource_id, expiration, sync_token, active, created_at = (
        stored[WORK]
    )
    assert calendar_id == WORK
    assert resource_id == f"resource-for-{WORK}"
    assert expiration == EXPIRATION_MS
    assert sync_token == f"sync-token-for-{WORK}"
    assert active == 1
    assert isinstance(created_at, int) and created_at > 0
    assert str(uuid.UUID(channel_id)) == channel_id
    assert WORK in out or "Fictional Work" in out


def test_events_watch_is_called_once_with_the_webhook_and_a_uuid_channel(google, run):
    run(WORK)
    (watch,) = google.watches
    assert watch["method"] == "POST"
    assert watch["path"] == f"/calendar/v3/calendars/{WORK}/events/watch"
    (body,) = google.watch_bodies()
    assert body["type"] == "web_hook"
    assert body["address"] == WEBHOOK
    assert str(uuid.UUID(body["id"])) == body["id"]
    assert rows()[WORK][1] == body["id"]


def test_events_are_read_in_full_without_expanding_series(google, run):
    google.event_pages[WORK] = [[{"id": "a"}], [{"id": "b"}], [{"id": "c"}]]
    google.sync_tokens[WORK] = "sync-after-the-last-page"
    run(WORK)
    reads = google.event_reads
    assert [r["query"].get("pageToken") for r in reads] == [
        None,
        "events-page-1",
        "events-page-2",
    ]
    for read in reads:
        assert "singleEvents" not in read["query"]
        assert "syncToken" not in read["query"]
    assert rows()[WORK][4] == "sync-after-the-last-page"


def test_each_attached_calendar_gets_its_own_channel_and_token(google, run):
    google.add(entry(HOME))
    run(WORK)
    run(HOME)
    stored = rows()
    assert stored[WORK][1] != stored[HOME][1]
    assert stored[WORK][2] != stored[HOME][2]
    assert stored[WORK][4] != stored[HOME][4]


def test_a_calendar_id_with_url_special_characters_is_stored_as_given(google, run):
    odd = "team+ops.c_abc123@group.calendar.google.com"
    google.add(entry(odd))
    status, _, _ = run(odd)
    assert status == 0
    assert list(rows()) == [odd]
    assert google.watches[0]["path"] == f"/calendar/v3/calendars/{odd}/events/watch"


def test_an_alias_is_stored_as_the_calendars_real_id(google, run):
    google.add(entry(HOME, summary="Fictional Primary"), alias="primary")
    status, _, _ = run("primary")
    assert status == 0
    assert list(rows()) == [HOME]


# --- 4. Refusals --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["writer", "reader", "freeBusyReader"])
def test_a_calendar_that_is_not_owned_is_refused(google, run, role):
    google.add(entry(SHARED, role=role))
    status, out, err = run(SHARED)
    assert status != 0
    assert SHARED in out + err
    assert rows() == {}
    assert google.watches == []
    assert google.event_reads == []


def test_a_calendar_without_an_access_role_is_refused(google, run):
    bare = entry(SHARED)
    del bare["accessRole"]
    google.add(bare)
    status, _, _ = run(SHARED)
    assert status != 0
    assert rows() == {}
    assert google.watches == []


def test_a_calendar_google_does_not_find_is_refused(google, run):
    status, out, err = run("nobody@example.org")
    assert status != 0
    assert "nobody@example.org" in out + err
    assert rows() == {}
    assert google.watches == []


# --- 5. Already attached ------------------------------------------------------------------


def test_an_active_calendar_is_reported_as_already_attached_and_left_alone(google, run):
    before = seed(WORK, active=1)
    status, out, err = run(WORK)
    assert status == 0
    assert "already" in (out + err).lower()
    assert rows()[WORK] == before
    assert google.writes == []


def test_attaching_the_same_calendar_twice_opens_one_subscription(google, run):
    run(WORK)
    first = rows()[WORK]
    status, _, _ = run(WORK)
    assert status == 0
    assert rows()[WORK] == first
    assert len(google.watches) == 1


def test_an_alias_for_an_active_calendar_is_recognised_as_attached(google, run):
    google.add(entry(HOME), alias="primary")
    before = seed(HOME, active=1)
    status, _, _ = run("primary")
    assert status == 0
    assert rows() == {HOME: before}
    assert google.watches == []


# --- 6. Re-activation ---------------------------------------------------------------------


def test_an_inactive_calendar_is_reactivated_with_a_fresh_subscription(google, run):
    seed(WORK, active=0)
    google.sync_tokens[WORK] = "fresh-cursor"
    status, _, _ = run(WORK)
    assert status == 0
    stored = rows()
    assert list(stored) == [WORK]
    _, channel_id, resource_id, expiration, sync_token, active, _ = stored[WORK]
    assert active == 1
    assert channel_id != "old-channel"
    assert str(uuid.UUID(channel_id)) == channel_id
    assert resource_id == f"resource-for-{WORK}"
    assert expiration == EXPIRATION_MS
    assert sync_token == "fresh-cursor"
    (body,) = google.watch_bodies()
    assert body["id"] == channel_id
    assert body["address"] == WEBHOOK


# --- 7. all and --auto-discover -----------------------------------------------------------


@ALL_FORMS
def test_every_owned_calendar_on_every_page_is_attached(mixed_list, run, arguments):
    status, out, _ = run(*arguments)
    assert status == 0
    assert set(rows()) == {WORK, HOME, SIDE}
    assert all(r[5] == 1 for r in rows().values())
    assert {b["address"] for b in mixed_list.watch_bodies()} == {WEBHOOK}
    assert len({b["id"] for b in mixed_list.watch_bodies()}) == 3
    for calendar_id in (WORK, HOME, SIDE):
        assert calendar_id in out


@ALL_FORMS
def test_calendars_that_are_not_owned_are_never_touched(mixed_list, run, arguments):
    run(*arguments)
    assert {calendar_of(w) for w in mixed_list.watches} == {WORK, HOME, SIDE}
    assert {calendar_of(r) for r in mixed_list.event_reads} == {WORK, HOME, SIDE}


@ALL_FORMS
def test_every_page_of_the_calendar_list_is_requested(mixed_list, run, arguments):
    run(*arguments)
    lists = [r for r in mixed_list.api_requests if r["path"].endswith("/calendarList")]
    assert [r["query"].get("pageToken") for r in lists] == [
        None,
        "list-page-1",
        "list-page-2",
    ]


@ALL_FORMS
def test_already_attached_calendars_are_skipped_and_reported(
    mixed_list, run, arguments
):
    before = seed(WORK, active=1)
    status, out, err = run(*arguments)
    assert status == 0
    assert rows()[WORK] == before
    assert set(rows()) == {WORK, HOME, SIDE}
    assert {calendar_of(w) for w in mixed_list.watches} == {HOME, SIDE}
    work_lines = [line for line in (out + err).splitlines() if WORK in line]
    assert work_lines
    assert all(
        "already" in line.lower() or "skip" in line.lower() for line in work_lines
    )


@ALL_FORMS
def test_an_inactive_owned_calendar_is_attached_again(mixed_list, run, arguments):
    seed(HOME, active=0)
    run(*arguments)
    assert rows()[HOME][5] == 1
    assert rows()[HOME][1] != "old-channel"


@ALL_FORMS
def test_a_failed_watch_is_reported_and_the_rest_still_attach(
    mixed_list, run, arguments
):
    mixed_list.watch_failures[WORK] = 500
    status, out, err = run(*arguments)
    assert status != 0
    assert set(rows()) == {HOME, SIDE}
    assert WORK in out + err
    assert HOME in out and SIDE in out


@ALL_FORMS
def test_a_failed_event_read_is_reported_and_the_rest_still_attach(
    mixed_list, run, arguments
):
    mixed_list.events_failures[WORK] = 503
    status, out, err = run(*arguments)
    assert status != 0
    assert set(rows()) == {HOME, SIDE}
    assert WORK in out + err
    assert WORK not in {calendar_of(w) for w in mixed_list.watches}


@ALL_FORMS
def test_a_failure_on_the_last_calendar_still_fails_the_command(
    mixed_list, run, arguments
):
    mixed_list.watch_failures[SIDE] = 500
    status, _, _ = run(*arguments)
    assert status != 0
    assert set(rows()) == {WORK, HOME}


def test_all_and_auto_discover_leave_the_same_state(mixed_list, run):
    def state():
        return {k: (v[0], v[2], v[3], v[4], v[5]) for k, v in rows().items()}

    run("all")
    by_all = state()
    wipe()
    run("--auto-discover")
    assert state() == by_all


def test_failing_to_list_calendars_exits_nonzero_and_writes_nothing(google, run):
    google.list_failure = 500
    status, _, _ = run("all")
    assert status != 0
    assert rows() == {}
    assert google.watches == []


# --- 8. Missing settings ------------------------------------------------------------------

REQUIRED = [
    "PUBLIC_BASE_URL",
    "GCP_CLIENT_ID",
    "GCP_CLIENT_SECRET",
    "GCP_REFRESH_TOKEN",
]


@pytest.mark.parametrize(
    "target", [WORK, "all", "--auto-discover"], ids=["one", "all", "auto-discover"]
)
@pytest.mark.parametrize("name", REQUIRED)
def test_a_missing_setting_refuses_before_any_google_call(
    google, run, monkeypatch, name, target
):
    monkeypatch.delenv(name)
    status, out, err = run(target)
    assert status != 0
    assert name in out + err
    assert google.requests == []
    assert rows() == {}


@pytest.mark.parametrize("name", REQUIRED)
def test_an_empty_setting_counts_as_missing(google, run, monkeypatch, name):
    monkeypatch.setenv(name, "")
    status, out, err = run(WORK)
    assert status != 0
    assert name in out + err
    assert google.requests == []
    assert rows() == {}


# --- 9. No other Google write, nothing evaluated ------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [[WORK], ["all"], ["--auto-discover"]],
    ids=["one", "all", "auto-discover"],
)
def test_the_only_google_write_is_events_watch(mixed_list, run, arguments):
    seed(HOME, active=0)
    run(*arguments)
    assert mixed_list.watches
    assert mixed_list.writes == mixed_list.watches
    assert not any("/channels/stop" in r["path"] for r in mixed_list.requests)


def test_only_the_documented_google_calls_are_made(mixed_list, run):
    run("all")
    allowed = (
        r"/calendar/v3/(users/me/calendarList(/[^/]+)?|calendars/[^/]+/events(/watch)?)"
    )
    for request in mixed_list.requests:
        assert request["path"] == "/token" or re.fullmatch(allowed, request["path"])


def test_overlaps_already_on_a_calendar_are_not_evaluated_or_dispatched(
    google, run, sentience
):
    google.add(entry(HOME))
    overlapping = [
        {
            "id": "one",
            "summary": "Fictional standup",
            "start": {"dateTime": "2030-01-07T10:00:00-05:00"},
            "end": {"dateTime": "2030-01-07T11:00:00-05:00"},
        },
        {
            "id": "two",
            "summary": "Fictional review",
            "start": {"dateTime": "2030-01-07T10:30:00-05:00"},
            "end": {"dateTime": "2030-01-07T11:30:00-05:00"},
        },
    ]
    google.event_pages[WORK] = [overlapping]
    google.event_pages[HOME] = [overlapping]
    status, _, _ = run("all")
    assert status == 0
    assert sentience == []
    assert execute("SELECT count(*) FROM processed_events")[0][0] == 0


# --- Argument forms -----------------------------------------------------------------------


def test_attach_without_a_target_is_a_usage_error_that_calls_nothing(google, run):
    status, _, err = run()
    assert status == 2
    assert err.strip()
    assert google.requests == []
    assert rows() == {}


# --- 10. README ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "needle",
    [
        "Desktop",
        "refresh token",
        "https://www.googleapis.com/auth/calendar.readonly",
        "GCP_CLIENT_ID",
        "GCP_CLIENT_SECRET",
        "GCP_REFRESH_TOKEN",
        "PUBLIC_BASE_URL",
    ],
)
def test_readme_covers_creating_the_google_credentials(needle):
    assert needle.lower() in (ROOT / "README.md").read_text().lower()


@pytest.mark.parametrize(
    "command",
    [
        "python -m dispatcher calendar attach <calendar_id>",
        "python -m dispatcher calendar attach all",
        "python -m dispatcher calendar attach --auto-discover",
    ],
    ids=["one", "all", "auto-discover"],
)
def test_readme_shows_each_form_of_calendar_attach(command):
    assert command in (ROOT / "README.md").read_text()
