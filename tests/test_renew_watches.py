import pytest
from fastapi.testclient import TestClient

from dispatcher.app import app

SECRET = "s3cret-example-value"
URL = "/internal/renew-watches"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_CRON_SECRET", SECRET)


def assert_rejected(response):
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.usefixtures("secret")
class TestWithSecret:
    def test_correct_bearer_token_is_accepted(self, client):
        response = client.post(URL, headers={"Authorization": f"Bearer {SECRET}"})
        assert 200 <= response.status_code < 300

    @pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr"])
    def test_scheme_is_case_insensitive(self, client, scheme):
        response = client.post(URL, headers={"Authorization": f"{scheme} {SECRET}"})
        assert 200 <= response.status_code < 300

    def test_no_authorization_header(self, client):
        assert_rejected(client.post(URL))

    @pytest.mark.parametrize(
        "header",
        [
            f"Basic {SECRET}",
            "Basic dXNlcjpwYXNz",
            f"Token {SECRET}",
            SECRET,
            f"Bearer{SECRET}",
            "Bearer",
            "Bearer ",
            "Bearer  ",
        ],
        ids=[
            "basic-secret",
            "basic-b64",
            "token-scheme",
            "bare",
            "no-space",
            "bearer-only",
            "empty",
            "blanks",
        ],
    )
    def test_malformed_or_other_scheme(self, client, header):
        assert_rejected(client.post(URL, headers={"Authorization": header}))

    @pytest.mark.parametrize(
        "token",
        [
            "x" * len(SECRET),
            "wrong",
            SECRET + "x",
            SECRET[:-1],
            SECRET.upper(),
            SECRET + " ",
            "x" * 4096,
        ],
        ids=[
            "same-length",
            "short",
            "secret-plus-char",
            "prefix",
            "upper",
            "trailing-space",
            "very-long",
        ],
    )
    def test_wrong_token(self, client, token):
        assert_rejected(client.post(URL, headers={"Authorization": f"Bearer {token}"}))

    def test_non_ascii_token_is_rejected_not_an_error(self, client):
        response = client.post(
            URL, headers={"Authorization": "Bearer café".encode("latin-1")}
        )
        assert_rejected(response)

    def test_rejection_does_not_reveal_the_secret(self, client):
        response = client.post(URL, headers={"Authorization": "Bearer nope"})
        assert SECRET not in response.text

    def test_a_get_does_not_renew_or_authenticate(self, client):
        response = client.get(URL, headers={"Authorization": f"Bearer {SECRET}"})
        assert response.status_code in (404, 405)


@pytest.mark.parametrize(
    "header",
    [None, "Bearer ", "Bearer anything", "Basic abc", "Bearer s3cret-example-value"],
    ids=["none", "empty-token", "any-token", "basic", "token"],
)
class TestWithoutSecret:
    def test_unset_secret_answers_500(self, client, header):
        headers = {} if header is None else {"Authorization": header}
        assert client.post(URL, headers=headers).status_code == 500

    def test_empty_secret_answers_500(self, client, header, monkeypatch):
        monkeypatch.setenv("INTERNAL_CRON_SECRET", "")
        headers = {} if header is None else {"Authorization": header}
        assert client.post(URL, headers=headers).status_code == 500


def test_secret_may_come_from_the_env_file(client, tmp_path):
    (tmp_path / ".env").write_text(f"INTERNAL_CRON_SECRET={SECRET}\n")
    response = client.post(URL, headers={"Authorization": f"Bearer {SECRET}"})
    assert 200 <= response.status_code < 300


def test_app_starts_with_no_database_setting(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL")
    monkeypatch.setenv("INTERNAL_CRON_SECRET", SECRET)
    with TestClient(app) as test_client:
        response = test_client.post(URL, headers={"Authorization": f"Bearer {SECRET}"})
    assert 200 <= response.status_code < 300


def test_app_startup_creates_the_schema(monkeypatch):
    monkeypatch.setenv("INTERNAL_CRON_SECRET", SECRET)
    with TestClient(app):
        pass
    from dispatcher import db

    conn = db.connect()
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert {"calendars", "sentience_auth", "processed_events"} <= tables
