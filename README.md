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

Tests replace the network by setting `dispatcher.http.transport` to an `httpx2` transport for Sentience's auth server and Resend, and `dispatcher.google_calendar.transport` to an `httplib2.Http`-compatible object for Google. They replace the operator's browser by replacing `dispatcher.cli.open_url`.

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

After a successful callback the app also processes the changes of every active calendar, one at a time, so conflicts that went undelivered while Sentience was disconnected are delivered.

## Keeping Sentience access alive

Before each delivery the app renews the stored access token with `grant_type=refresh_token` if it has lapsed or lapses within 60 seconds, using the lifetime Sentience states for it (a token stated with no lifetime is renewed before every use). A new refresh token replaces the old one in the same statement that stores the new access token; without one the old token is kept. If Sentience's MCP server answers `401`, the app renews once and retries the delivery once.

When Sentience refuses the refresh token with `invalid_grant` (status `400` or `401`), the app marks access expired and emails `ALERT_TO` through Resend, once per expiry, from `Calendar Deconflicter <onboarding@resend.dev>`, with the subject `[Calendar Deconflicter] Alert — Sentience needs re-authorization` and a link to `<PUBLIC_BASE_URL>/oauth/sentience/reauth`. The email carries no event data. A failed send is logged and not retried. Any other refresh failure leaves the conflict undelivered and marks nothing expired.

While access is expired the app neither refreshes nor calls Sentience, every conflict is undelivered (its notification answers `503`), and `POST /internal/renew-watches` renews every channel and then answers `503`. `GET /oauth/sentience/reauth` then redirects to a new Sentience sign-in; at any other time it starts nothing and answers `409`. A successful callback clears the expired mark. `auth sentience` reconnects at any time.

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

The endpoint answers `200` when every calendar was renewed, or when none is attached. It answers `502` when any calendar failed, after renewing the others, so the workflow's run fails. While Sentience access is expired it answers `503` after renewing (see Keeping Sentience access alive). A calendar whose new channel could not be opened keeps its old one. An old channel that Google no longer knows (`404`) is not a failure; any other failed close is, and the new channel is still stored. The response body names no calendar, because the workflow's log is public. It answers `500` when a setting it needs is unset: `INTERNAL_CRON_SECRET`, the database, and, once a calendar is attached, `PUBLIC_BASE_URL` and the Google settings.

## Detach a calendar

```bash
uv run python -m dispatcher calendar detach <calendar_id>
```

The command closes the calendar's Google channel and marks the calendar inactive. The row stays, and `calendar attach` can attach it again. If Google no longer knows the channel (`404`), the calendar is still marked inactive. A calendar that is not attached, or is already detached, is reported and Google is not called. If Google fails to close the channel, the calendar stays attached and the command exits nonzero, so you can run it again. The command writes nothing to Google except closing the channel.

## Change notifications

`POST /webhooks/gcal` is the address every attached calendar's Google channel notifies. The `X-Goog-Channel-ID` header says which calendar changed, and the app reads that calendar's changes with `events.list` from the stored `sync_token`, every page, delivers the conflicts they raise, then stores the new `sync_token`. It never writes to Google.

- `200`: the changes were read and every conflict in them is delivered or was delivered before.
- `400`: the request has no `X-Goog-Channel-ID`.
- `404`: no active calendar has that channel, so nothing is read. A detached calendar's old channel is one of these.
- `503`, with the stored `sync_token` unchanged: Google, the connection to it, or a delivery to Sentience failed, so Google retries the notification.
- When Google answers `410` because it no longer honors the `sync_token`, the app clears it, reads every event to get a fresh one, stores it, and answers `200`. If that read fails, the `sync_token` stays cleared and the next notification for the calendar does the full read.

## Conflicts

For each changed event, the app asks Google for the events that overlap it on every attached calendar, the changed event's own included, and keeps no copy of any calendar's events. A cancelled event and a transparent ("Free") one are left out, both as the changed event and as the one it overlaps. An all-day event that isn't transparent is the interval from midnight on its `start.date` to midnight on its `end.date`, in the primary calendar's time zone. Times are half-open, so meetings that only touch don't conflict, and a conflict whose overlap has already ended is not raised. A meeting never conflicts with itself, with its own series or occurrences, or with another copy of its invitation, such as the same meeting on two calendars.

A recurring series is one meeting. It overlaps another event when any of its occurrences does, and another series when any occurrence of one overlaps any occurrence of the other. The app reads occurrences from Google's `events.instances`, and never computes them from the recurrence rule. An occurrence that was moved or edited on its own is an event in its own right and no longer an occurrence of its series, and a cancelled occurrence doesn't count. A new or changed series is checked against every upcoming single event, however far ahead, over that event's interval, and against every other series over the next 90 days; two series whose first collision lies further out raise nothing until one of them changes. A new or changed single event is checked against every series with an occurrence in its interval. A colliding pair is one conflict, which describes the earliest colliding occurrence that hasn't ended, and it is delivered once: its de-duplication hash holds the series' own `start`, `end`, and `recurrence`, so it is delivered again only when a series' schedule or classification changes. A conflict between events on two calendars has `cross_calendar: true`, and both events are Internal whoever is invited; either calendar's notification finds it, and it is delivered once.

Each event is classified `internal` or `external_mixed`: external when its organizer or any attendee, other than a room or the operator's own calendar, has an email domain that isn't `GOOGLE_WORKSPACE_DOMAIN` (compared without regard to case; a subdomain is outside). Two external events call for the `irreconcilable` policy, one of each for `never_move_external`, and two internal ones for `full_evaluation`.

Every overlapping pair is one conflict, delivered as one call of Sentience's `ask_sentience` tool over a Streamable HTTP session to `SENTIENCE_MCP_URL`, with the stored access token and a single `message` argument. The message is the fixed instruction, the policy's sentence, and a JSON payload of the two events, with times in the primary calendar's time zone, as the PRD's Conflict Message Contract defines. It carries no description, location, conference link, or attachment. A call that returns a result that isn't an error delivers the conflict, and the app then records the conflict's hash in `processed_events`; Sentience's reply is not read or kept. A conflict whose hash is recorded is not sent again, so a repeated notification, or a title change that leaves the classification as it was, dispatches nothing. A change to either event's time or classification does. If Sentience isn't connected, its access is expired, or a call fails, that conflict isn't recorded and the notification is answered `503`; the conflicts delivered before it stay recorded.

Notifications are processed one at a time across the app, so each waits for the one before it.
