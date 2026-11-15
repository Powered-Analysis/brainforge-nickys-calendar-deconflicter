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

Tests replace the network by setting `dispatcher.http.transport` to an `httpx` transport for Sentience, and `dispatcher.google_calendar.transport` to an `httplib2.Http`-compatible object for Google. They replace the operator's browser by replacing `dispatcher.cli.open_url`.

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

## Connect Google (First-Time Setup, step 2)

The app calls the Calendar API as you, with a Desktop OAuth client and a refresh token. It asks for one scope, `https://www.googleapis.com/auth/calendar.readonly`, which every call it makes accepts. Creating the client takes a few minutes in the Google Cloud console:

1. In a Google Cloud project, enable the **Google Calendar API**.
2. Under **APIs & Services → OAuth consent screen**, configure the app for your Workspace. Choose *Internal* if it offers the choice, so the refresh token does not expire after seven days.
3. Under **Credentials**, create an **OAuth client ID** of type **Desktop app**. Its client ID and client secret are `GCP_CLIENT_ID` and `GCP_CLIENT_SECRET`.
4. Obtain a refresh token for your own account with that client and the scope above. Run the Desktop flow once on your machine, for example with Google's [OAuth 2.0 Playground](https://developers.google.com/oauthplayground) set to *Use your own OAuth credentials*, authorizing `calendar.readonly` with *offline* access. The token it returns is `GCP_REFRESH_TOKEN`.

Fill `GCP_CLIENT_ID`, `GCP_CLIENT_SECRET`, and `GCP_REFRESH_TOKEN` in your `.env` and as Fly secrets. Also fill `PUBLIC_BASE_URL` and `TURSO_DATABASE_URL`, which `calendar attach` needs too.

## Connect Sentience (First-Time Setup, step 3)

With the app deployed and `PUBLIC_BASE_URL`, `SENTIENCE_AUTH_ISSUER`, and the database settings in your `.env`, run on your own machine:

```bash
uv run python -m dispatcher auth sentience
```

The command registers the dispatcher with Sentience as an OAuth client, the first time only, and stores the client in the database. It then prints Sentience's sign-in URL and opens it in your browser. After you sign in, Sentience redirects the browser to `<PUBLIC_BASE_URL>/oauth/sentience/callback`. The deployed app exchanges the code for tokens, stores them in the database, and shows *Sentience Connected Successfully.* Run the command again at any time to sign in again; it reuses the stored client.

The command exits nonzero, and opens no browser, if registration fails. The callback answers `400` when Sentience reports an error or the authorization is unknown or already used, and `502` when Sentience's token endpoint does not return tokens; the stored tokens are unchanged in both cases. The app's access log omits the callback's query string, so the authorization code is never logged.

## Attach calendars (First-Time Setup, step 4)

With the app deployed, run on your own machine:

```bash
uv run python -m dispatcher calendar attach <calendar_id>           # one calendar; `primary` works
uv run python -m dispatcher calendar attach all                     # every calendar you own
uv run python -m dispatcher calendar attach --auto-discover         # the same as `all`
```

For each calendar you own, the command reads its events once to record a starting point, opens a Google notification channel that points at `<PUBLIC_BASE_URL>/webhooks/gcal`, and stores the calendar in the database. It checks nothing for conflicts at that moment: an overlap already on the calendar is raised the next time either meeting changes. No redeploy or restart is needed, and the command writes nothing to Google except the notification channel.

- A calendar that is already attached is skipped, and nothing changes.
- A calendar you do not own, or that is not on your calendar list, is refused, and the command exits nonzero.
- A calendar that was detached is attached again, with a new channel and starting point.
- With `all`, a failure on one calendar is reported and the rest still attach; the command exits nonzero if any failed.
- The command refuses, before calling Google, if `PUBLIC_BASE_URL` or a Google setting is unset, and the message names it.

## Keep monitoring running (First-Time Setup, step 5)

Google's notification channels expire, so the workflow `.github/workflows/renew-watches.yml` calls `POST /internal/renew-watches` every 3 days. For each attached calendar, the endpoint opens a new channel at `<PUBLIC_BASE_URL>/webhooks/gcal`, stores it, and then closes the old one. The calendar's starting point is unchanged, and detached calendars are left alone.

The endpoint answers `200` when every calendar was renewed, or when none is attached. It answers `502` when any calendar failed, after renewing the others, so the workflow's run fails. A calendar whose new channel could not be opened keeps its old one. An old channel that Google no longer knows (`404`) is not a failure; any other failed close is, and the new channel is still stored. The response body names no calendar, because the workflow's log is public. It answers `500` when a setting it needs is unset: `INTERNAL_CRON_SECRET`, the database, and, once a calendar is attached, `PUBLIC_BASE_URL` and the Google settings.

## Detach a calendar

```bash
uv run python -m dispatcher calendar detach <calendar_id>
```

The command closes the calendar's Google channel and marks the calendar inactive. The row stays, and `calendar attach` can attach it again. If Google no longer knows the channel (`404`), the calendar is still marked inactive. A calendar that is not attached, or is already detached, is reported and Google is not called. If Google fails to close the channel, the calendar stays attached and the command exits nonzero, so you can run it again. The command writes nothing to Google except closing the channel.

## Change notifications

`POST /webhooks/gcal` is the address every attached calendar's Google channel notifies. The `X-Goog-Channel-ID` header says which calendar changed, and the app reads that calendar's changes with `events.list` from the stored `sync_token`, every page, then stores the new `sync_token`. It never writes to Google.

- `200`: the changes were read.
- `400`: the request has no `X-Goog-Channel-ID`.
- `404`: no active calendar has that channel, so nothing is read. A detached calendar's old channel is one of these.
- `503`, with the stored `sync_token` unchanged: Google or the connection to it failed, so Google retries the notification.
- When Google answers `410` because it no longer honors the `sync_token`, the app clears it, reads every event to get a fresh one, stores it, and answers `200`. If that read fails, the `sync_token` stays cleared and the next notification for the calendar does the full read.

Notifications are processed one at a time across the app, so each waits for the one before it.
