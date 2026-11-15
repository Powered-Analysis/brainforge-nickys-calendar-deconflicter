# Calendar Deconflicter

A dispatcher that watches every Google Calendar its operator owns, detects overlapping commitments, classifies each conflict by who is in the meetings, and hands it to the operator's Sentience agent over MCP. Sentience decides what to recommend and how to tell the operator. The dispatcher never changes an event, an RSVP, or a mailbox.

The product is specified in [`prds/00-overview.md`](prds/00-overview.md).

## How it is built

An autonomous four-agent team builds the app on `main`, one issue and one pull request at a time, by the operating agreement in [`docs/agentic_coding_team.md`](docs/agentic_coding_team.md). The owner's code conventions are in [`docs/conventions.md`](docs/conventions.md), and the owner's settings for the team are in [`team.yml`](team.yml).

## Settings

[`.env.example`](.env.example) lists every setting the app reads. Copy it to `.env` and fill it in; `.env` is gitignored.
