# Autonomous Multi-Calendar Conflict Resolution Agent (Sentience + Google Workspace MCP)

#### tl;dr

A zero-DevOps, reactive scheduling pipeline that intercepts overlapping Google Calendar invitations across any user-owned calendar, processes all-day busy blocks and cross-calendar overlaps, and dispatches a stateful Sentience agent via remote MCP to evaluate rescheduling feasibility. Sentience independently governs meeting inertia and horizon tuning—strictly enforcing that External or Mixed meetings are never recommended to move over Internal meetings—and queries candidate openings via Google Calendar MCP's `suggest_time`. Sentience directly determines how to surface actionable recommendations or explicit "Irreconcilable Conflict" advisories to the operator, while the pipeline guarantees zero automated calendar mutations or outbound communications.

---

#### Goals

##### Business Goals

* **Zero-DevOps Calendar Provisioning:** Onboard any new calendar owned by the user in under 10 seconds via a single CLI command or auto-discovery call, requiring zero infrastructure redeployments, configuration edits, or environment variable modifications.
* **Triage Latency:** Deliver structured conflict analyses and candidate alternative slots to the Sentience agent within 30 seconds of an incoming overlapping invitation hitting any registered calendar.
* **Zero Unauthorized Mutations:** Maintain a 100% human-in-the-loop guarantee—zero automated calendar RSVP updates (no automated `tentative`, `declined`, or `accepted` mutations) and zero automated outbound emails or drafts.
* **Full Conflict Visibility:** Ensure 100% of genuine time collisions—including all-day events marked "Busy" and cross-calendar overlaps—are surfaced to the user via Sentience.
* **Irreconcilable Triage Completeness:** Classify and surface 100% of non-reschedulable conflicts (e.g., External-vs-External overlaps or high-inertia Internal meetings) as actionable, first-class irreconcilable events.

##### User Goals

* **Frictionless Multi-Calendar Coverage:** Effortlessly extend conflict monitoring across primary, secondary, client-specific, or newly created Google Calendars without managing separate infrastructure stacks.
* **Preserve External Commitments:** Enforce an invariant rule that an External or Mixed meeting is never recommended to be rescheduled over an Internal meeting.
* **Autonomous Surface & Inertia Management:** Rely on the stateful Sentience agent to govern meeting inertia, search horizons, and user notification delivery without hardcoding fragile application rules.
* **First-Class Irreconcilable Triage:** Receive immediate, structured notifications when a conflict cannot be rescheduled (e.g., two external meetings colliding or immovable internal meetings) to enable rapid manual triage.

##### Non-Goals

* **Autonomous Outbound Actions:** No automated sending of emails, drafting of messages in Gmail, or mutation of event RSVP states.
* **Application-Level Inertia Rules:** The dispatcher app will not enforce hardcoded rules for VIPs, stakeholder seniority, or attendee thresholds; all inertia heuristics belong exclusively to Sentience.
* **Application-Level Notification UI:** The dispatcher does not manage client-side UI, desktop notification daemons, or messaging webhooks; Sentience selects and executes its own surfacing mechanism.
* **Headless Browser Scraping:** No automation or reverse-engineering of Google Calendar’s web UI or the proprietary "Propose a new time" web button.
* **Multi-Tenant SaaS Management:** Designed specifically as a single-operator pipeline running on personal and internal Workspace infrastructure.

---

#### User Stories

##### Persona: Multi-Calendar Operator (Founder, Executive, Technical Lead)

* As a Multi-Calendar Operator, I want to attach any calendar I own to the monitoring service with a single command, so that I do not have to touch Dockerfiles, Fly.io configs, or environment variables when my calendar footprint expands.
* As a Multi-Calendar Operator, I want all-day events marked "Busy" (`transparency: "opaque"`) to generate conflicts against overlapping meetings, so that out-of-office days and travel blocks are fully protected.
* As a Multi-Calendar Operator, I want cross-calendar collisions between my own calendars treated as Internal-vs-Internal conflicts, so that my personal and secondary commitments are balanced against my primary work schedule.
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

##### Sentience Agent Decision Domain & Delivery (Priority: P0)

* **Inertia & VIP Guidelines (P0):** Sentience maintains user-defined operational guidelines for meeting inertia, VIP prioritization, and stakeholder weighting within its own stateful configuration. The dispatcher application remains strictly agnostic to inertia logic.
* **Tunable Lookahead Horizon (P0):** The scheduling search horizon (defaulting to 5 business days) is managed and tuned directly within the stateful Sentience agent.
* **Calendar MCP Availability Lookup (`suggest_time`) (P0):** When evaluating a moveable internal meeting, Sentience connects to `calendarmcp.googleapis.com` to query mutual availability for all internal attendees across its active horizon.
* **Autonomous Surfacing of Outcomes (P0):** Sentience determines how to deliver the recommendation or advisory to the user (e.g., Slack DM, macOS notification, daily briefing, or conversational prompt). The dispatcher app has no opinion or role in rendering user-facing alerts.
* *Outcome 1 (Suggested Reschedule):* Identifies the moveable internal meeting, provides 2–3 validated candidate windows, and outlines required manual user steps.
* *Outcome 2 (Irreconcilable Conflict):* Issued when two External/Mixed meetings collide, when an Internal meeting possesses too much inertia to move, or when zero alternative slots exist within the horizon.


* **Zero Mutation Guarantee (P0):** The pipeline performs no write operations against Google Calendar RSVP statuses and creates no draft or outbound communications.

##### Cloud Infrastructure & Serverless Scale-to-Zero (Priority: P0)

* **Fly.io Suspend Configuration (P0):** Deploy the ingress dispatcher on Fly.io with:
```toml
[http_service]
  auto_stop_machines = "suspend"
  auto_start_machines = true
  min_machines_running = 0

```


Ensures the machine scales to zero when idle ($0 CPU/$0 RAM) while the Fly Proxy wakes it via memory snapshot in 200–400 ms upon incoming webhook requests or OAuth callbacks.
* **External Watch Renewal Ping (P0):** An external runner pings `POST /internal/renew-watches` every 3 days to wake the suspended machine, renew all active Google Calendar watch channels in Turso, and allow it to suspend again.
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


4. User registers all owned calendars:
```bash
python -m dispatcher calendar attach --auto-discover

```



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
* **Sentience Re-Authentication Failure (Degraded Auth):** If Sentience revokes the refresh token (`401 invalid_grant`), the dispatcher marks auth as expired in Turso and generates an operational alert with a re-auth link: `https://<app>.fly.dev/oauth/sentience/reauth`.
* **410 Gone Sync Invalidation:** If Google invalidates a calendar’s `syncToken`, catch the 410 error, clear the token in Turso, perform an immediate full sync to capture a fresh cursor, and resume delta monitoring without raising false alarms.

---

#### Technical Considerations

* **Separation of Concerns:** The dispatcher is an unopinionated pipeline: it detects overlaps, enforces domain classifications, verifies all-day event transparency, and invokes Sentience over MCP. All heuristics regarding lookahead horizons, meeting inertia, VIP sensitivity, and notification presentation live inside the stateful Sentience agent.
* **All-Day Event Arithmetic:** Google Calendar all-day events provide `start.date` and `end.date` (exclusive). For conflict detection, the dispatcher normalizes an opaque all-day event into a localized datetime interval spanning `start.dateT00:00:00` to `end.dateT00:00:00` in the user's primary calendar timezone.
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
* *Role:* Manage `events.watch` subscriptions, query `CalendarList` for discovery, and fetch deltas via `syncToken`.
* *Library:* `google-api-python-client` / `httpx`.


* **Google Remote Calendar MCP (`calendarmcp.googleapis.com`):**
* *Role:* Execute attendee availability queries via `suggest_time`.
* *Protocol:* Model Context Protocol (Streamable HTTP / SSE).


* **Turso Cloud (libSQL):**
* *Role:* Managed serverless database for multi-calendar state, Sentience OAuth tokens, channel tokens, and deduplication hashes.
* *Driver:* `turso-serverless` over HTTPS. Every connection is opened with `isolation_level = None`: the driver's DB-API `autocommit` attribute is stored and never read, and under the default isolation level every write opens an implicit transaction that `close()` discards. With `isolation_level = None` each statement commits on its own, so a write that must be atomic (Token Rotation Safety) is a single statement or an explicit `BEGIN … COMMIT` on one connection.


* **Fly.io Ingress Host:**
* *Role:* Public webhook receiver and OAuth callback handler configured with `auto_stop_machines = "suspend"`.
* *Stack:* Python, FastAPI, `uv`.


* **External Cron Runner (GitHub Actions / Cloudflare Worker):**
* *Role:* Scheduled wake ping triggering `POST /internal/renew-watches` every 3 days.



---

#### Outstanding Questions

* **Sentience Token Longevity:** What is the specific TTL and rotation policy on Sentience refresh tokens, and under what conditions does Sentience invalidate unattended agent refresh tokens?
* **Cross-Calendar Ingress Timing:** When an invite arrives on Calendar A that conflicts with Calendar B, should the dispatcher query Calendar B's events on-demand during delta processing, or maintain a consolidated cache of all monitored calendar intervals in Turso?
