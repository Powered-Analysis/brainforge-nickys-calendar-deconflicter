import uuid

import pytest
import turso_serverless

from dispatcher import db

# The PRD's Database Schema (Turso): (name, type, not null, primary key), in column order.
EXPECTED = {
    "calendars": [
        ("calendar_id", "TEXT", 0, 1),
        ("channel_id", "TEXT", 1, 0),
        ("resource_id", "TEXT", 1, 0),
        ("channel_expiration", "INTEGER", 1, 0),
        ("sync_token", "TEXT", 0, 0),
        ("active", "INTEGER", 0, 0),
        ("created_at", "INTEGER", 1, 0),
    ],
    "sentience_auth": [
        ("id", "TEXT", 0, 1),
        ("client_id", "TEXT", 1, 0),
        ("access_token", "TEXT", 1, 0),
        ("refresh_token", "TEXT", 0, 0),
        ("expires_at", "INTEGER", 1, 0),
        ("updated_at", "INTEGER", 1, 0),
    ],
    "processed_events": [
        ("event_hash", "TEXT", 0, 1),
        ("calendar_id", "TEXT", 1, 0),
        ("processed_at", "INTEGER", 1, 0),
    ],
}


def columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [(r[1], r[2].upper(), r[3], r[5]) for r in rows]


@pytest.fixture
def conn():
    db.ensure_schema()
    connection = db.connect()
    yield connection
    connection.close()


@pytest.mark.parametrize("table", sorted(EXPECTED))
def test_table_has_the_prd_columns(conn, table):
    assert columns(conn, table) == EXPECTED[table]


def test_calendars_active_defaults_to_one(conn):
    calendar_id = f"cal-{uuid.uuid4().hex}@example.com"
    conn.execute(
        "INSERT INTO calendars (calendar_id, channel_id, resource_id,"
        " channel_expiration, created_at) VALUES (?, 'c', 'r', 1, 1)",
        (calendar_id,),
    )
    try:
        row = conn.execute(
            "SELECT active, sync_token FROM calendars WHERE calendar_id = ?",
            (calendar_id,),
        ).fetchone()
        assert tuple(row) == (1, None)
    finally:
        conn.execute("DELETE FROM calendars WHERE calendar_id = ?", (calendar_id,))


def test_primary_keys_reject_duplicates(conn):
    event_hash = uuid.uuid4().hex
    insert = "INSERT INTO processed_events VALUES (?, 'cal@example.com', 1)"
    conn.execute(insert, (event_hash,))
    try:
        with pytest.raises(Exception):  # noqa: B017 - the driver's error type is its own
            conn.execute(insert, (event_hash,))
    finally:
        conn.execute("DELETE FROM processed_events WHERE event_hash = ?", (event_hash,))


def test_not_null_columns_reject_null(conn):
    with pytest.raises(Exception):  # noqa: B017
        conn.execute(
            "INSERT INTO processed_events VALUES (?, NULL, 1)", (uuid.uuid4().hex,)
        )


def test_schema_setup_is_idempotent_and_keeps_data():
    db.ensure_schema()
    event_hash = uuid.uuid4().hex
    first = db.connect()
    first.execute(
        "INSERT INTO processed_events VALUES (?, 'cal@example.com', 7)", (event_hash,)
    )
    first.close()
    try:
        db.ensure_schema()
        db.ensure_schema()
        second = db.connect()
        rows = second.execute(
            "SELECT calendar_id, processed_at FROM processed_events"
            " WHERE event_hash = ?",
            (event_hash,),
        ).fetchall()
        assert [tuple(r) for r in rows] == [("cal@example.com", 7)]
        for table, expected in EXPECTED.items():
            assert columns(second, table) == expected
        second.close()
    finally:
        cleanup = db.connect()
        cleanup.execute(
            "DELETE FROM processed_events WHERE event_hash = ?", (event_hash,)
        )
        cleanup.close()


def test_a_row_written_through_the_module_is_visible_from_a_fresh_connection():
    db.ensure_schema()
    event_hash = uuid.uuid4().hex
    writer = db.connect()
    writer.execute(
        "INSERT INTO processed_events VALUES (?, 'cal@example.com', 3)", (event_hash,)
    )
    # The writer stays open and never commits explicitly: the write must already be durable.
    try:
        reader = db.connect()
        rows = reader.execute(
            "SELECT processed_at FROM processed_events WHERE event_hash = ?",
            (event_hash,),
        ).fetchall()
        reader.close()
        assert [tuple(r) for r in rows] == [(3,)]
    finally:
        writer.execute(
            "DELETE FROM processed_events WHERE event_hash = ?", (event_hash,)
        )
        writer.close()


def test_connections_are_opened_with_autocommit_isolation(monkeypatch):
    seen = []
    real = turso_serverless.connect

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(turso_serverless, "connect", spy)
    db.connect().close()
    db.ensure_schema()
    assert seen
    assert all(kwargs.get("isolation_level", "unset") is None for kwargs in seen)


def test_empty_auth_token_sends_no_token(monkeypatch):
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "")
    seen = []
    real = turso_serverless.connect

    def spy(*args, **kwargs):
        seen.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(turso_serverless, "connect", spy)
    connection = db.connect()
    connection.execute("SELECT 1").fetchall()
    connection.close()
    for args, kwargs in seen:
        token = kwargs.get("auth_token", args[1] if len(args) > 1 else None)
        assert not token


def test_connecting_without_a_database_url_fails_clearly(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL")
    with pytest.raises(Exception):  # noqa: B017
        db.connect()
