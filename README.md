# Calendar Deconflicter

A dispatcher that watches every Google Calendar its operator owns, detects overlapping commitments, classifies each conflict by who is in the meetings, and hands it to the operator's Sentience agent over MCP. Sentience decides what to recommend and how to tell the operator. The dispatcher never changes an event, an RSVP, or a mailbox.

The product is specified in [`prds/00-overview.md`](prds/00-overview.md).

## How it is built

An autonomous four-agent team builds the app on `main`, one issue and one pull request at a time, by the operating agreement in [`docs/agentic_coding_team.md`](docs/agentic_coding_team.md). The owner's code conventions are in [`docs/conventions.md`](docs/conventions.md), and the owner's settings for the team are in [`team.yml`](team.yml).

## Settings

[`.env.example`](.env.example) lists every setting the app reads. Copy it to `.env` and fill it in; `.env` is gitignored.

## Install and test

```bash
uv sync
uv run python -m dispatcher --help
uv run pytest <path>        # needs TURSO_DATABASE_URL, e.g. a local libSQL server
bash scripts/suite.sh       # the full suite, one libSQL container per CPU (needs Docker)
uv run ruff format && uv run ruff check
```

A local libSQL server that takes no token: `docker run -p 8080:8080 ghcr.io/tursodatabase/libsql-server:latest`, with `TURSO_DATABASE_URL=http://127.0.0.1:8080`.

## Run

```bash
uv run uvicorn dispatcher.app:app --port 8080
```

When `TURSO_DATABASE_URL` is set, the app creates its tables on startup if they are missing.

## Build the image

```bash
docker build -t dispatcher .
docker run --rm -p 8080:8080 --env-file .env dispatcher
```

## Deploy to Fly.io

The app runs as one machine that suspends when idle and wakes on the next request, so deploy it without a second machine. `fly.toml` holds the service configuration and leaves the app's name to Fly.

```bash
fly launch --copy-config --no-deploy      # pick the app's name and region
fly secrets set --stage \
  GCP_CLIENT_ID=... GCP_CLIENT_SECRET=... GCP_REFRESH_TOKEN=... GOOGLE_WORKSPACE_DOMAIN=... \
  TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... PUBLIC_BASE_URL=... INTERNAL_CRON_SECRET=... \
  RESEND_API_KEY=... ALERT_TO=... SENTIENCE_MCP_URL=... SENTIENCE_AUTH_ISSUER=...
fly deploy --ha=false
```

The secrets are the names in [`.env.example`](.env.example). `POST /internal/renew-watches` answers `500` until `INTERNAL_CRON_SECRET` is set, and `401` to any request that does not carry `Authorization: Bearer <that secret>`.
