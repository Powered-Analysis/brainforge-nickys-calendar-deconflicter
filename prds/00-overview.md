# Autonomous Multi-Calendar Conflict Resolution Agent (Sentience + Google Workspace MCP)

#### tl;dr

A zero-DevOps, reactive scheduling pipeline that intercepts overlapping Google Calendar invitations across any user-owned calendar, processes all-day busy blocks and cross-calendar overlaps, and dispatches a stateful Sentience agent via remote MCP to evaluate rescheduling feasibility. Sentience independently governs meeting inertia and horizon tuning—strictly enforcing that External or Mixed meetings are never recommended to move over Internal meetings—and queries candidate openings via Google Calendar MCP's `suggest_time`. Sentience directly determines how to surface actionable recommendations or explicit "Irreconcilable Conflict" advisories to the operator, while the pipeline guarantees zero automated calendar mutations or communications on the operator's behalf.

---

#### Goals

##### Business Goals

* **Zero-DevOps Calendar Provisioning:** Onboard any new calendar owned by the user in under 10 seconds via a single CLI command or auto-discovery call, requiring zero infrastructure redeployments, configuration edits, or environment variable modifications.
* **Triage Latency:** Deliver structured conflict analyses and candidate alternative slots to the Sentience agent within 30 seconds of an incoming overlapping invitation hitting any registered calendar.
* **Zero Unauthorized Mutations:** Maintain a 100% human-in-the-loop guarantee—zero automated calendar RSVP updates (no automated `tentative`, `declined`, or `accepted` mutations) and zero automated emails or drafts to anyone but the operator.
* **Full Conflict Visibility:** Ensure 100% of genuine time collisions—including all-day events marked "Busy" and cross-calendar overlaps—are surfaced to the user via Sentience.
* **Irreconcilable Triage Completeness:** Classify and surface 100% of non-reschedulable conflicts (e.g., External-vs-External overlaps or high-inertia Internal meetings) as actionable, first-class irreconcilable events.

##### User Goals

* **Frictionless Multi-Calendar Coverage:** Effortlessly extend conflict monitoring across primary, secondary, client-specific, or newly created Google Calendars without managing separate infrastructure stacks.
* **Preserve External Commitments:** Enforce an invariant rule that an External or Mixed meeting is never recommended to be rescheduled over an Internal meeting.
* **Autonomous Surface & Inertia Management:** Rely on the stateful Sentience agent to govern meeting inertia, search horizons, and user notification delivery without hardcoding fragile application rules.
* **First-Class Irreconcilable Triage:** Receive immediate, structured notifications when a conflict cannot be rescheduled (e.g., two external meetings colliding or immovable internal meetings) to enable rapid manual triage.

##### Non-Goals

* **Autonomous Outbound Actions:** No automated sending of emails to attendees or to anyone else on the operator's behalf, drafting of messages in Gmail, or mutation of event RSVP states.
* **Application-Level Inertia Rules:** The dispatcher app will not enforce hardcoded rules for VIPs, stakeholder seniority, or attendee thresholds; all inertia heuristics belong exclusively to Sentience.
* **Application-Level Notification UI:** The dispatcher does not manage client-side UI, desktop notification daemons, or messaging webhooks; Sentience selects and executes its own surfacing mechanism. The one message the dispatcher sends itself is the Re-Authorization Alert, which cannot go through Sentience.
* **Headless Browser Scraping:** No automation or reverse-engineering of Google Calendar’s web UI or the proprietary "Propose a new time" web button.
* **Multi-Tenant SaaS Management:** Designed specifically as a single-operator pipeline running on personal and internal Workspace infrastructure.

---

#### User Stories

##### Persona: Multi-Calendar Operator (Founder, Executive, Technical Lead)

* As a Multi-Calendar Operator, I want to attach any calendar I own to the monitoring service with a single command, so that I do not have to touch Dockerfiles, Fly.io configs, or environment variables when my calendar footprint expands.
* As a Multi-Calendar Operator, I want all-day events marked "Busy" (`transparency: "opaque"`) to generate conflicts against overlapping meetings, so that out-of-office days and travel blocks are fully protected.
* As a Multi-Calendar Operator, I want cross-calendar collisions between my own calendars treated as Internal-vs-Internal conflicts, so that my personal and secondary commitments are balanced against my primary work schedule.
* As a Multi-Calendar Operator, I want a recurring meeting treated as one commitment, so that two series that collide every week raise one conflict instead of one a week.
* As a Multi-Calendar Operator, I want all conflicts surfaced to me through Sentience, ensuring it never recommends moving an External/Mixed meeting over an Internal meeting.
* As a Multi-Calendar Operator, I want collisions between two External or Mixed meetings surfaced as "Irreconcilable Conflicts," so that I can immediately decide which commitment to decline or adjust manually.
* As a Multi-Calendar Operator, I want to establish high-inertia guidelines and tune lookahead horizons directly within my stateful Sentience agent, so that scheduling judgment compounds inside the agent rather than in application code.

---

#### Functional Requirements

##### Multi-Calendar Registration & Zero-DevOps Onboarding (Priority: P0)

* **Dynamic Calendar Onboarding CLI (P0):** A lightweight management command (`python -m dispatcher calendar attach <calendar_id_or_all>`) that:
1. Verifies ownership permissions via Google Calendar API `CalendarList.get`.
2. Creates a dedicated webhook subscription (`events.watch`) with the central ingress URL.
3. Saves the calendar ID, resource ID, channel ID, and base `syncToken` to Turso.


* **Auto-Discovery Mode (P1):** A one-step discovery flag (`--auto-discover`) that queries `CalendarList.list`, filters for calendars where `accessRole == "owner"`, and registers all unmonitored calendars in a single batch.
* **Centralized Multiplexed Ingress (P0):** A single Fly.io webhook endpoint (`POST /webhooks/gcal`) that inspects the incoming `X-Goog-Channel-ID` header, maps it dynamically to the corresponding calendar record in Turso, and processes the event delta under that calendar's scope.
* **Calendar Teardown Command (P1):** A clean detach command (`python -m dispatcher calendar detach <calendar_id>`) that calls `channels.stop` with Google Calendar API and marks the calendar inactive in Turso.

##### Ingress & Deterministic Conflict Detection (Priority: P0)

* **SyncToken Delta Processing (P0):** On webhook notification, query `Calendar.Events.list` for the target calendar using its persisted `syncToken`, isolating created or updated events without historical scanning.
* **All-Day "Busy" Event Evaluation (P0):** Evaluate all-day events (defined by `start.date` and `end.date`) as full-day conflicting intervals (`[00:00:00, 23:59:59]` in the user's primary timezone) whenever `transparency` is `"opaque"` (or unset, which defaults to opaque). Only events explicitly set to `transparency: "transparent"` (Free) are excluded.
* **Cross-Calendar Collision Detection (P0):** When an incoming event on Calendar A overlaps with an existing event on Calendar B (where both calendars are owned and monitored by the user), the system flags the collision and tags both events as **Internal**.
* **Recurring Series as One Commitment (P0):** A recurring series is one event, however many occurrences it has. Two series that collide every week therefore produce one conflict, not one a week.
  * *Reading:* Deltas are read without expanding series (`singleEvents` is not set). Google then returns a series as one event carrying `recurrence`, and returns an occurrence that was moved or edited on its own as a separate event carrying `recurringEventId`. That separate event is an ordinary event in its own right and no longer counts as an occurrence of the series. Neither does a cancelled occurrence.
  * *Overlap:* A series overlaps another event when any of its occurrences does, and overlaps another series when any occurrence of one overlaps any occurrence of the other. Occurrences are read from Google with `events.instances`, never computed from the recurrence rule.
  * *Look-ahead:* Against a single event, the occurrences examined are those within that event's interval. Against another series, they are the occurrences of both from the moment the delta is processed until 90 days later. 90 days is a first cut, to be revisited once real calendars show how far ahead two series first collide. *Accepted gap:* two series whose first collision lies beyond that window raise no conflict until either one changes.
  * *Classification:* A series is classified by the attendees of the series event.
  * *The conflict* describes the earliest colliding occurrence (Conflict Message Contract) and is de-duplicated by the series' schedule, not by that occurrence's date (Conflict De-duplication). It is dispatched once, and again only when a series' schedule or classification changes. *Accepted cost:* the operator gets one notice per colliding pair. Resolving one occurrence by hand leaves the series colliding, and its later occurrences are not raised again.
* **Attendee Domain Classification (P0):** For single-calendar events, classify each overlapping event deterministically:
* **Internal:** 100% of attendee email domains match the user's primary Google Workspace domain.
* **External / Mixed:** One or more attendees have email domains outside the user's Google Workspace domain.


* **Deterministic Dispatch Rules (P0):**
* *All genuine conflicts must be dispatched to Sentience via MCP.*
* *External/Mixed vs. External/Mixed:* Flagged to Sentience as an **Irreconcilable Conflict**. Sentience surfaces the conflict to the user for manual resolution.
* *External/Mixed vs. Internal:* Flagged to Sentience with the strict constraint: **Never recommend moving the External/Mixed meeting**. Sentience evaluates whether the Internal meeting can be moved or whether internal inertia renders the conflict irreconcilable.
* *Internal vs. Internal (including Cross-Calendar):* Dispatched to Sentience for full inertia evaluation and rescheduling suggestions.



##### Sentience Remote MCP Client & Auth Lifecycle (Priority: P0)

* **Dynamic Client Registration Handler (P0):** On initial deployment, register the client with Sentience at `https://api.sentience.com/oauth/mcp-issuer/register` using `client_name` and the Fly callback URL (`https://<app>.fly.dev/oauth/sentience/callback`). Store issued `client_id` in Turso.
* **PKCE Authorization Flow (P0):**
* Generate `code_verifier` and SHA-256 `code_challenge`.
* Redirect operator to `https://api.sentience.com/oauth/mcp-issuer/authorize` with `scope=sentience` and `response_type=code`.
* Ingress route `GET /oauth/sentience/callback` receives the authorization code, completes token exchange against `https://api.sentience.com/oauth/mcp-issuer/token`, and persists tokens in Turso.


* **Token Rotation & Refresh Management (P0):**
* Prior to dispatching an MCP call, inspect token expiration.
* Execute token renewal via `grant_type=refresh_token`. If a replacement refresh token is returned, update Turso atomically.


* **Remote MCP Session Dispatch (P0):** Initialize an MCP client connection to `https://mcp.sentience.com/mcp` using Streamable HTTP, authenticating requests with `Authorization: Bearer <access_token>`. Dispatch the structured conflict payload.
* **Dispatch Tool Call (P0):** A conflict is one pair of overlapping events, so an event that overlaps several others produces one conflict per pair. A recurring series counts as one event (Recurring Series as One Commitment). Each conflict is dispatched as one `tools/call` of Sentience's `ask_sentience` tool with two arguments: `message`, holding the text defined under Conflict Message Contract, and `thread_id`, the value of the required setting `SENTIENCE_THREAD_ID`. Every conflict therefore lands in the one existing Sentience chat the operator chose, instead of opening a new chat that the operator never sees. The operator supplies the id as a Fly secret, so it never appears in the repository. A missing or empty `SENTIENCE_THREAD_ID` is a dispatch failure (see Delivery): the dispatcher never calls `ask_sentience` without a `thread_id`. The dispatcher calls no other Sentience tool: it never calls `read_thread`, and it stores no thread id.
* **Delivery (P0):** A conflict is delivered when the tool call returns a result that is not an error. Sentience's reply and its "still pending" note both count, and the dispatcher reads neither: what Sentience decides is never parsed, stored, or acted on. A conflict's `processed_events` entry is written only once the conflict is delivered. When the call fails (a transport error, an authorization failure that a token refresh does not cure, an error result such as Sentience rejecting the `thread_id`, or a missing or empty `SENTIENCE_THREAD_ID`), the conflict is not recorded, the calendar's `syncToken` is not advanced, and the webhook answers `503`. Google retries a `503`, and any later notification for that calendar re-reads the same delta, so an undelivered conflict is dispatched again and a delivered one is not.
* **Re-Authorization Alert (P0):** When a token refresh fails with `invalid_grant`, the dispatcher marks Sentience auth as expired and emails the operator, because Sentience is the one channel that is then closed.
  * *The email* is sent through Resend's API with the key `RESEND_API_KEY`, to the address `ALERT_TO`, from `Calendar Deconflicter <onboarding@resend.dev>`, with the subject `[Calendar Deconflicter] Alert — Sentience needs re-authorization`. The sender and the subject prefix are those of the build team's reports, so the operator's existing mail filter catches it. The body is one sentence saying that conflicts are not being delivered, and the re-auth link `<PUBLIC_BASE_URL>/oauth/sentience/reauth`. It carries no event data.
  * *One email per expiry:* it is sent when auth becomes expired, and not again while auth stays expired. A send that fails is logged and changes nothing else.
  * *The standing signal:* while auth is expired, `POST /internal/renew-watches` still renews every channel and then answers `503`, so every scheduled run of the renewal workflow fails until the operator re-authorizes.
  * *Recovery:* when re-authorization succeeds, the dispatcher processes the delta of every active calendar, so the conflicts that went undelivered are dispatched.

##### Sentience Agent Decision Domain & Delivery (Priority: P0)

* **Inertia & VIP Guidelines (P0):** Sentience maintains user-defined operational guidelines for meeting inertia, VIP prioritization, and stakeholder weighting within its own stateful configuration. The dispatcher application remains strictly agnostic to inertia logic.
* **Tunable Lookahead Horizon (P0):** The scheduling search horizon (defaulting to 5 business days) is managed and tuned directly within the stateful Sentience agent.
* **Calendar MCP Availability Lookup (`suggest_time`) (P0):** When evaluating a moveable internal meeting, Sentience connects to `calendarmcp.googleapis.com` to query mutual availability for all internal attendees across its active horizon.
* **Autonomous Surfacing of Outcomes (P0):** Sentience determines how to deliver the recommendation or advisory to the user (e.g., Slack DM, macOS notification, daily briefing, or conversational prompt). The dispatcher app has no opinion or role in rendering user-facing alerts.
* *Outcome 1 (Suggested Reschedule):* Identifies the moveable internal meeting, provides 2–3 validated candidate windows, and outlines required manual user steps.
* *Outcome 2 (Irreconcilable Conflict):* Issued when two External/Mixed meetings collide, when an Internal meeting possesses too much inertia to move, or when zero alternative slots exist within the horizon.


* **Zero Mutation Guarantee (P0):** The pipeline performs no write operations against Google Calendar RSVP statuses and creates no draft. It sends nothing to anyone but the operator, and its one message to the operator is the Re-Authorization Alert.

##### Cloud Infrastructure & Serverless Scale-to-Zero (Priority: P0)

* **Fly.io Suspend Configuration (P0):** Deploy the ingress dispatcher on Fly.io with:
```toml
[http_service]
  auto_stop_machines = "suspend"
  auto_start_machines = true
  min_machines_running = 0

```


Ensures the machine scales to zero when idle ($0 CPU/$0 RAM) while the Fly Proxy wakes it via memory snapshot in 200–400 ms upon incoming webhook requests or OAuth callbacks.
* **External Watch Renewal Ping (P0):** A scheduled GitHub Actions workflow in this repository, `.github/workflows/renew-watches.yml`, pings `POST /internal/renew-watches` every 3 days to wake the suspended machine, renew all active Google Calendar watch channels in Turso, and allow it to suspend again. The workflow takes the app's origin and the bearer token from the repository secrets `PUBLIC_BASE_URL` and `INTERNAL_CRON_SECRET`, and its run fails on any answer that is not 2xx. It is enabled as the last step of First-Time Setup. *Accepted risk:* GitHub disables a scheduled workflow in a public repository after 60 days without repository activity. From then on no channel is renewed, and conflicts stop being detected once the channels expire, until the operator re-enables the workflow.
* **Renewal Ping Authentication (P0):** `POST /internal/renew-watches` accepts only a request that carries `Authorization: Bearer <token>` with a token equal to `INTERNAL_CRON_SECRET`. The two are compared in constant time, so the time a rejection takes reveals nothing about the secret. A request whose token is missing, malformed, or wrong gets `401` with `WWW-Authenticate: Bearer` and renews nothing. When `INTERNAL_CRON_SECRET` is unset or empty, every request gets `500` and renews nothing, so a deployment that lacks the secret is never open.
* **Turso (libSQL) Data Layer (P0):** Store multi-calendar state, sync cursors, watch channels, Sentience OAuth tokens, and deduplication logs in Turso.

---

#### User Experience

**Entry Point & First-Time Setup**

1. User provisions a Turso database and deploys the ingress container to Fly.io with `auto_stop_machines = "suspend"`.
2. User authenticates Google OAuth credentials (using a Desktop OAuth client, storing the Google refresh token in Fly secrets).
3. User connects Sentience:
```bash
python -m dispatcher auth sentience

```


* The CLI registers the client at Sentience's MCP issuer and opens the browser to `https://api.sentience.com/oauth/mcp-issuer/authorize`.
* User signs in; browser redirects to `https://<app>.fly.dev/oauth/sentience/callback`.
* Fly Machine wakes up, exchanges the PKCE code for tokens, writes them to Turso, and displays: *"Sentience Connected Successfully."*
* The operator sets the chat conflicts are delivered to, with the id of an existing Sentience chat: `fly secrets set SENTIENCE_THREAD_ID=<thread id>`.


4. User registers all owned calendars:
```bash
python -m dispatcher calendar attach --auto-discover

```



5. User schedules the renewal ping, once the app is deployed and its calendars are attached. The schedule is the workflow `.github/workflows/renew-watches.yml`, which stays disabled until this step:
```bash
gh secret set PUBLIC_BASE_URL        # the app's origin, as in .env
gh secret set INTERNAL_CRON_SECRET   # the same value as the Fly secret
gh workflow enable renew-watches.yml
gh workflow run renew-watches.yml    # one run now, which must succeed

```


* Until this step is done, nothing renews the watch channels that step 4 created, and each stops notifying when it expires.



**Adding a New Calendar (Zero-DevOps Flow)**
When the user creates or acquires a new calendar:

```bash
python -m dispatcher calendar attach new-calendar-id@group.calendar.google.com

```

* **System Action:** Turso records the new calendar, calls `events.watch`, and begins listening immediately on the existing Fly.io webhook URL. Zero redeployments or container restarts required.

**Core Experience**

* **Step 1 (Ingest):** A conflicting invitation or all-day busy block arrives on any monitored calendar. Google pushes a notification to `https://<app>.fly.dev/webhooks/gcal`.
* **Step 2 (Wake & Route):** Fly Proxy wakes the suspended Machine in ~300 ms. The dispatcher retrieves the calendar record from Turso and queries deltas using `syncToken`.
* **Step 3 (Deterministic Filter & Tagging):**
* Check event transparency: Ignore only if `transparency: "transparent"`. All-day opaque events are mapped to full-day busy blocks.
* Cross-calendar overlap detected? Tag both events as **Internal**.
* Classify attendee domains (Internal vs. External/Mixed).


* **Step 4 (Sentience Remote MCP Call):** Dispatcher establishes a Streamable HTTP MCP session with `https://mcp.sentience.com/mcp`, passing the collision payload and strict policy tags.
* **Step 5 (Sentience Agent Reasoning & Canvassing):**
* *Two External/Mixed:* Sentience marks as Irreconcilable.
* *External/Mixed vs. Internal:* Sentience checks internal inertia. If moveable, Sentience calls `suggest_time` via `calendarmcp.googleapis.com` within its tuned horizon. If high inertia, marks as Irreconcilable.
* *Internal vs. Internal:* Sentience compares relative inertia and searches for slots for the more flexible meeting.


* **Step 6 (Surfacing the Decision):** Sentience uses its configured delivery channel to present the recommendation or irreconcilable notice directly to the user.
* **Step 7 (User Action):** The operator manually resolves the conflict in Google Calendar.

**Edge Cases & Operational Recovery**

* **External-vs-External Collision:** Both meetings involve third parties. Sentience immediately surfaces an "Irreconcilable Conflict" briefing advising the user to choose which meeting to decline.
* **All-Day Opaque Event Collision:** An invitation arrives overlapping with an all-day "Board Offsite" (marked Busy). The system treats the entire day as blocked and flags the conflict to Sentience.
* **Cross-Calendar Collision:** A personal hold on Calendar B collides with an internal sync on Calendar A. Both are treated as Internal, allowing Sentience to evaluate which is more flexible.
* **Sentience Re-Authentication Failure (Degraded Auth):** If Sentience revokes the refresh token (`401 invalid_grant`), the dispatcher marks auth as expired in Turso and sends the Re-Authorization Alert with its re-auth link: `https://<app>.fly.dev/oauth/sentience/reauth`.
* **410 Gone Sync Invalidation:** If Google invalidates a calendar’s `syncToken`, catch the 410 error, clear the token in Turso, perform an immediate full sync to capture a fresh cursor, and resume delta monitoring without raising false alarms.

---

#### Technical Considerations

* **Separation of Concerns:** The dispatcher is an unopinionated pipeline: it detects overlaps, enforces domain classifications, verifies all-day event transparency, and invokes Sentience over MCP. All heuristics regarding lookahead horizons, meeting inertia, VIP sensitivity, and notification presentation live inside the stateful Sentience agent.
* **All-Day Event Arithmetic:** Google Calendar all-day events provide `start.date` and `end.date` (exclusive). For conflict detection, the dispatcher normalizes an opaque all-day event into a localized datetime interval spanning `start.dateT00:00:00` to `end.dateT00:00:00` in the user's primary calendar timezone.
* **Conflict Message Contract:** The `message` argument of `ask_sentience` is plain text in three parts, in this order, each separated from the next by one blank line.

  1. *The instruction*, the same for every conflict:
```text
Automated message from the calendar conflict dispatcher. Two of the operator's commitments overlap. Handle this conflict under the operator's standing guidelines for calendar conflicts and surface the outcome to the operator. Do not change either event, respond to either invitation, or contact any attendee.
```

  2. *The policy sentence* for the conflict, chosen by the classifications of its two events. `policy` is the stored value the payload carries; the sentence is sent verbatim.

| Conflict | `policy` | Policy sentence |
|---|---|---|
| External/Mixed vs. External/Mixed | `irreconcilable` | `Policy: Irreconcilable Conflict. Both meetings are External or Mixed. Recommend moving neither, and surface the conflict for the operator to resolve manually.` |
| External/Mixed vs. Internal | `never_move_external` | `Policy: never recommend moving the External or Mixed meeting. Decide whether the Internal meeting can move, or whether its inertia makes the conflict irreconcilable.` |
| Internal vs. Internal, which includes every cross-calendar conflict | `full_evaluation` | `Policy: both meetings are Internal. Weigh their inertia and suggest new times for the more flexible one.` |

  3. *The payload:* a fenced `json` code block holding one JSON object with exactly these fields. The closing fence ends the message.
```json
{
  "policy": "never_move_external",
  "cross_calendar": false,
  "overlap": {"start": "2026-03-02T10:00:00-05:00", "end": "2026-03-02T10:30:00-05:00"},
  "incoming": {
    "calendar_id": "operator@example.com",
    "event_id": "4kq1t8f0example",
    "summary": "Vendor review",
    "start": "2026-03-02T10:00:00-05:00",
    "end": "2026-03-02T11:00:00-05:00",
    "all_day": false,
    "recurring": false,
    "classification": "external_mixed",
    "organizer": "pat@example.org",
    "attendees": ["operator@example.com", "pat@example.org"]
  },
  "existing": {
    "calendar_id": "operator@example.com",
    "event_id": "7hd02mzexample",
    "summary": "Team sync",
    "start": "2026-03-02T09:30:00-05:00",
    "end": "2026-03-02T10:30:00-05:00",
    "all_day": false,
    "recurring": false,
    "classification": "internal",
    "organizer": "operator@example.com",
    "attendees": ["operator@example.com", "sam@example.com"]
  }
}
```

  * `incoming` is the created or updated event the delta returned, and `existing` is the event it overlaps.
  * `classification` is `internal` (displayed as Internal) or `external_mixed` (displayed as External / Mixed). In a cross-calendar conflict both events are `internal`, and `cross_calendar` is `true`.
  * `start`, `end`, and both ends of `overlap` are RFC 3339 timestamps carrying the UTC offset of the user's primary calendar timezone. For an all-day event `all_day` is `true`, and `start` and `end` are the interval All-Day Event Arithmetic gives it.
  * `recurring` is `true` for a recurring series. Its `event_id` is then the id of the series, and its `start` and `end` are those of its earliest colliding occurrence, as is `overlap`.
  * `summary` is the event's title, or `""` when Google returns none. `organizer` is the organizer's email address, or `null` when Google returns none. `attendees` holds every attendee's email address in the order Google returns them, and is `[]` for an event without attendees.
  * Nothing else about an event is sent: no description, location, conference link, or attachment. An invitation's text is written by whoever sent it, so the dispatcher passes Sentience the identifiers it needs to look the event up, and no free text beyond the title.
* **Database Schema (Turso):**
```sql
CREATE TABLE calendars (
    calendar_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    channel_expiration INTEGER NOT NULL,
    sync_token TEXT,
    active INTEGER DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE sentience_auth (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE processed_events (
    event_hash TEXT PRIMARY KEY,
    calendar_id TEXT NOT NULL,
    processed_at INTEGER NOT NULL
);

```


* **Conflict De-duplication (`event_hash`):** `event_hash` identifies one conflict in the state it was dispatched in. It is the lowercase hex SHA-256 of a UTF-8 string built from six fields of each of the conflict's two events: `calendar_id`, `event_id`, `start`, `end`, `recurrence`, and `classification`.
  * `calendar_id`, `event_id`, and `classification` are those of the Conflict Message Contract.
  * `start` and `end` are written in UTC as `YYYY-MM-DDTHH:MM:SSZ`. For a recurring series they are the series event's own `start` and `end`, which are those of its first occurrence, and not the colliding occurrence's.
  * `recurrence` is the series event's `recurrence` lines in the order Google returns them, joined with a single space. It is empty for an event that does not recur.
  * The two events are taken in ascending order of (`calendar_id`, `event_id`), and all twelve fields are joined with `|`.
  * The same two events with unchanged times and classifications give the same hash, so a repeated notification, a replayed delta, and the full sync after a `410` dispatch nothing twice. The order makes the hash the same whichever event was the incoming one.
  * A change to either event's time, recurrence rule, or classification gives a new hash, so the changed conflict is dispatched again. A change to a title or to attendees that leaves the classification as it was does not.
  * The date of a series' colliding occurrence is not in the hash, so the passing of time never dispatches a series conflict again.
  * The row's `calendar_id` is the incoming event's calendar, and `processed_at` is when the conflict was delivered.
  * For the example payload of the Conflict Message Contract, the string is `operator@example.com|4kq1t8f0example|2026-03-02T15:00:00Z|2026-03-02T16:00:00Z||external_mixed|operator@example.com|7hd02mzexample|2026-03-02T14:30:00Z|2026-03-02T15:30:00Z||internal` and the hash is `fe51fe1ff6c549b16f83b3c01c01ca3652734fb3dfc27cf34edb6bf25fbc143d`.
  * Had "Team sync" been a weekly series that began on 2026-01-05 with the rule `RRULE:FREQ=WEEKLY;BYDAY=MO`, the string would be `operator@example.com|4kq1t8f0example|2026-03-02T15:00:00Z|2026-03-02T16:00:00Z||external_mixed|operator@example.com|7hd02mzexample|2026-01-05T14:30:00Z|2026-01-05T15:30:00Z|RRULE:FREQ=WEEKLY;BYDAY=MO|internal` and the hash `32118fb0b34f3641bfbd1dde3cddd11f183d0318b3749c662d02f9d342228852`.
* **Dual OAuth Model:**
* *Google Workspace:* Standard Desktop Client OAuth (offline refresh token in Fly secrets). No public Google redirect URIs required.
* *Sentience MCP:* RFC 7591 Dynamic Client Registration and RFC 7636 Authorization Code with PKCE. Uses public redirect URI `GET /oauth/sentience/callback` on Fly.io.


* **Token Rotation Safety:** Sentience token refresh occurs inside an atomic Turso transaction. If a new `refresh_token` is returned by `/oauth/mcp-issuer/token`, the old refresh token is immediately replaced.

---

#### Integration Points

* **Sentience Remote MCP Server (`https://mcp.sentience.com/mcp`):**
* *Role:* Reasoning engine for meeting inertia, horizon tuning, candidate slot selection, and user notification delivery.
* *Transport:* Streamable HTTP over HTTPS.
* *Auth:* OAuth 2.0 PKCE with Bearer token (`scope=sentience`).


* **Sentience Auth Server (`https://api.sentience.com`):**
* *Role:* Dynamic Client Registration (`/oauth/mcp-issuer/register`), authorization (`/oauth/mcp-issuer/authorize`), token exchange/refresh (`/oauth/mcp-issuer/token`), and revocation (`/oauth/mcp-issuer/revoke`).


* **Google Calendar API v3 (`calendar-json.googleapis.com`):**
* *Role:* Manage `events.watch` subscriptions, query `CalendarList` for discovery, fetch deltas via `syncToken`, and read a recurring series' occurrences via `events.instances`.
* *Library:* `google-api-python-client` / `httpx`.


* **Google Remote Calendar MCP (`calendarmcp.googleapis.com`):**
* *Role:* Execute attendee availability queries via `suggest_time`.
* *Protocol:* Model Context Protocol (Streamable HTTP / SSE).


* **Resend (`https://api.resend.com`):**
* *Role:* Delivers the Re-Authorization Alert to the operator's inbox.
* *Library:* `httpx`.
* *Auth:* `RESEND_API_KEY`, a key with sending access only.


* **Turso Cloud (libSQL):**
* *Role:* Managed serverless database for multi-calendar state, Sentience OAuth tokens, channel tokens, and deduplication hashes.
* *Driver:* `turso-serverless` over HTTPS. Every connection is opened with `isolation_level = None`: the driver's DB-API `autocommit` attribute is stored and never read, and under the default isolation level every write opens an implicit transaction that `close()` discards. With `isolation_level = None` each statement commits on its own, so a write that must be atomic (Token Rotation Safety) is a single statement or an explicit `BEGIN … COMMIT` on one connection.


* **Fly.io Ingress Host:**
* *Role:* Public webhook receiver and OAuth callback handler configured with `auto_stop_machines = "suspend"`.
* *Stack:* Python, FastAPI, `uv`.


* **External Cron Runner (GitHub Actions):**
* *Role:* Scheduled wake ping triggering `POST /internal/renew-watches` every 3 days, from `.github/workflows/renew-watches.yml`.
* *Auth:* The bearer secret defined under Renewal Ping Authentication, held as the repository secret `INTERNAL_CRON_SECRET`.
* *Config:* The repository secret `PUBLIC_BASE_URL`, the app's origin.



---

#### Outstanding Questions

* **Sentience Token Longevity:** What is the specific TTL and rotation policy on Sentience refresh tokens, and under what conditions does Sentience invalidate unattended agent refresh tokens?
* **Cross-Calendar Ingress Timing:** When an invite arrives on Calendar A that conflicts with Calendar B, should the dispatcher query Calendar B's events on-demand during delta processing, or maintain a consolidated cache of all monitored calendar intervals in Turso?
