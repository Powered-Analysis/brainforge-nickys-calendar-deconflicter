"""The one point of database access for the whole app."""

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
)


def connect() -> turso_serverless.Connection:
    url = settings.get("TURSO_DATABASE_URL")
    if not url:
        raise RuntimeError("TURSO_DATABASE_URL is not set")
    # Integration Points, Turso: with isolation_level None each statement commits on its own.
    return turso_serverless.connect(
        url,
        auth_token=settings.get("TURSO_AUTH_TOKEN") or None,
        isolation_level=None,
    )


def ensure_schema() -> None:
    conn = connect()
    try:
        for statement in SCHEMA:
            conn.execute(statement)
    finally:
        conn.close()
