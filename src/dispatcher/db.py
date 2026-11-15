"""The one point of database access for the whole app."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import NamedTuple

import turso_serverless

from dispatcher import settings

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS calendars (
        calendar_id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        channel_expiration INTEGER NOT NULL,
        sync_token TEXT,
        active INTEGER DEFAULT 1,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sentience_auth (
        id TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        access_token TEXT NOT NULL,
        refresh_token TEXT,
        expires_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_events (
        event_hash TEXT PRIMARY KEY,
        calendar_id TEXT NOT NULL,
        processed_at INTEGER NOT NULL
    )
    """,
    # The registered OAuth client. sentience_auth cannot hold it: its tokens are NOT NULL and
    # do not exist until the operator has signed in.
    """
    CREATE TABLE IF NOT EXISTS sentience_client (
        id TEXT PRIMARY KEY,
        client_id TEXT NOT NULL
    )
    """,
    # A row here means Sentience has refused the refresh token. sentience_auth's columns are
    # fixed by the PRD, so the mark has a table of its own.
    """
    CREATE TABLE IF NOT EXISTS sentience_auth_expired (
        id TEXT PRIMARY KEY,
        expired_at INTEGER NOT NULL
    )
    """,
    # An authorization the CLI started and the callback has not yet finished.
    """
    CREATE TABLE IF NOT EXISTS sentience_pending_authorization (
        state TEXT PRIMARY KEY,
        code_verifier TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """,
)


class Channel(NamedTuple):
    """An active calendar and the Google notification channel that watches it."""

    calendar_id: str
    channel_id: str
    resource_id: str


class StoredAuth(NamedTuple):
    client_id: str
    access_token: str
    refresh_token: str | None
    expires_at: int


# The key of the single row in sentience_auth, sentience_auth_expired, and sentience_client.
SENTIENCE_ID = "sentience"


def connect() -> turso_serverless.Connection:
    # Integration Points, Turso: with isolation_level None each statement commits on its own.
    return turso_serverless.connect(
        settings.require("TURSO_DATABASE_URL"),
        auth_token=settings.get("TURSO_AUTH_TOKEN") or None,
        isolation_level=None,
    )


@contextmanager
def _connection() -> Iterator[turso_serverless.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema() -> None:
    with _connection() as conn:
        for statement in SCHEMA:
            conn.execute(statement)


def is_calendar_active(calendar_id: str) -> bool:
    with _connection() as conn:
        row = conn.execute(
            "SELECT active FROM calendars WHERE calendar_id = ?", (calendar_id,)
        ).fetchone()
    return bool(row and row[0])


def get_active_channel(calendar_id: str) -> Channel | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT calendar_id, channel_id, resource_id FROM calendars "
            "WHERE calendar_id = ? AND active = 1",
            (calendar_id,),
        ).fetchone()
    return Channel(*row) if row else None


def get_active_channels() -> list[Channel]:
    with _connection() as conn:
        rows = conn.execute(
            "SELECT calendar_id, channel_id, resource_id FROM calendars "
            "WHERE active = 1 ORDER BY calendar_id"
        ).fetchall()
    return [Channel(*row) for row in rows]


def replace_channel(
    calendar_id: str, channel_id: str, resource_id: str, channel_expiration: int
) -> None:
    with _connection() as conn:
        conn.execute(
            "UPDATE calendars SET channel_id = ?, resource_id = ?, channel_expiration = ? "
            "WHERE calendar_id = ? AND active = 1",
            (channel_id, resource_id, channel_expiration, calendar_id),
        )


def deactivate_calendar(calendar_id: str) -> None:
    """Stop monitoring a calendar but keep its row."""
    with _connection() as conn:
        conn.execute(
            "UPDATE calendars SET active = 0 WHERE calendar_id = ?", (calendar_id,)
        )


def get_active_calendar_by_channel(channel_id: str) -> tuple[str, str | None] | None:
    """The calendar ID and `syncToken` of the active calendar that owns this channel."""
    with _connection() as conn:
        row = conn.execute(
            "SELECT calendar_id, sync_token FROM calendars "
            "WHERE channel_id = ? AND active = 1",
            (channel_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def save_sync_token(calendar_id: str, sync_token: str | None) -> None:
    with _connection() as conn:
        conn.execute(
            "UPDATE calendars SET sync_token = ? WHERE calendar_id = ?",
            (sync_token, calendar_id),
        )


def get_sentience_auth() -> StoredAuth | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT client_id, access_token, refresh_token, expires_at "
            "FROM sentience_auth WHERE id = ?",
            (SENTIENCE_ID,),
        ).fetchone()
    return StoredAuth(*row) if row else None


def save_refreshed_tokens(
    access_token: str, refresh_token: str | None, expires_at: int, updated_at: int
) -> None:
    """Token Rotation Safety: one statement, so a new refresh token replaces the old one
    together with the new access token or not at all. Without a new refresh token the old one
    is kept."""
    with _connection() as conn:
        conn.execute(
            "UPDATE sentience_auth SET access_token = ?, "
            "refresh_token = COALESCE(?, refresh_token), expires_at = ?, updated_at = ? "
            "WHERE id = ?",
            (access_token, refresh_token, expires_at, updated_at, SENTIENCE_ID),
        )


def is_sentience_auth_expired() -> bool:
    with _connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM sentience_auth_expired WHERE id = ?", (SENTIENCE_ID,)
        ).fetchone()
    return row is not None


def mark_sentience_auth_expired(expired_at: int) -> bool:
    """False when auth was already marked expired, so only the call that marks it alerts."""
    with _connection() as conn:
        return (
            conn.execute(
                "INSERT OR IGNORE INTO sentience_auth_expired (id, expired_at) VALUES (?, ?)",
                (SENTIENCE_ID, expired_at),
            ).rowcount
            == 1
        )


def is_conflict_processed(event_hash: str) -> bool:
    with _connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_events WHERE event_hash = ?", (event_hash,)
        ).fetchone()
    return row is not None


def record_processed_conflict(
    event_hash: str, calendar_id: str, processed_at: int
) -> None:
    with _connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_events (event_hash, calendar_id, processed_at) "
            "VALUES (?, ?, ?)",
            (event_hash, calendar_id, processed_at),
        )


def save_attached_calendar(
    calendar_id: str,
    channel_id: str,
    resource_id: str,
    channel_expiration: int,
    sync_token: str,
    created_at: int,
) -> None:
    """Store a newly attached calendar, or give a detached one a fresh subscription and cursor."""
    with _connection() as conn:
        conn.execute(
            "INSERT INTO calendars (calendar_id, channel_id, resource_id, channel_expiration, "
            "sync_token, active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(calendar_id) DO UPDATE SET channel_id = excluded.channel_id, "
            "resource_id = excluded.resource_id, "
            "channel_expiration = excluded.channel_expiration, "
            "sync_token = excluded.sync_token, active = 1",
            (
                calendar_id,
                channel_id,
                resource_id,
                channel_expiration,
                sync_token,
                created_at,
            ),
        )


def get_sentience_client_id() -> str | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT client_id FROM sentience_client WHERE id = ?", (SENTIENCE_ID,)
        ).fetchone()
    return row[0] if row else None


def save_sentience_client_id(client_id: str) -> None:
    with _connection() as conn:
        conn.execute(
            "INSERT INTO sentience_client (id, client_id) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET client_id = excluded.client_id",
            (SENTIENCE_ID, client_id),
        )


def save_pending_authorization(state: str, code_verifier: str, created_at: int) -> None:
    with _connection() as conn:
        conn.execute(
            "INSERT INTO sentience_pending_authorization (state, code_verifier, created_at) "
            "VALUES (?, ?, ?)",
            (state, code_verifier, created_at),
        )


def get_pending_code_verifier(state: str) -> str | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT code_verifier FROM sentience_pending_authorization WHERE state = ?",
            (state,),
        ).fetchone()
    return row[0] if row else None


def store_sentience_tokens(
    state: str,
    client_id: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: int,
    updated_at: int,
) -> bool:
    """Replace the stored tokens, clear the expired mark, and consume the pending
    authorization, atomically.

    False, with nothing written, when the authorization was already consumed: two callbacks
    for one state cannot both store tokens.
    """
    with _connection() as conn:
        conn.execute("BEGIN")
        try:
            consumed = (
                conn.execute(
                    "DELETE FROM sentience_pending_authorization WHERE state = ?",
                    (state,),
                ).rowcount
                == 1
            )
            if consumed:
                conn.execute(
                    "DELETE FROM sentience_auth_expired WHERE id = ?", (SENTIENCE_ID,)
                )
                conn.execute(
                    "INSERT INTO sentience_auth "
                    "(id, client_id, access_token, refresh_token, expires_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET client_id = excluded.client_id, "
                    "access_token = excluded.access_token, "
                    "refresh_token = excluded.refresh_token, "
                    "expires_at = excluded.expires_at, updated_at = excluded.updated_at",
                    (
                        SENTIENCE_ID,
                        client_id,
                        access_token,
                        refresh_token,
                        expires_at,
                        updated_at,
                    ),
                )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT" if consumed else "ROLLBACK")
    return consumed
