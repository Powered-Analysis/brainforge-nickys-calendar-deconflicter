# Calendar Deconflicter — PRD Set

This directory specifies the **Calendar Deconflicter**: a dispatcher that detects conflicts across every Google Calendar its operator owns and hands each one to the operator's Sentience agent, which decides what to recommend and how to surface it.

The PRDs are **living documents**: present-tense, rewritten in place, with the git log as the archive. They follow the writing and decomposition discipline in [`PRINCIPLES.md`](PRINCIPLES.md).

## The set

| PRD | Scope |
|---|---|
| [00 — Overview](00-overview.md) | The whole product: calendar registration, ingress and conflict detection, the Sentience MCP client and its auth lifecycle, Sentience's decision domain, and the cloud infrastructure |

## Conventions

- **One fact, one home.** A PRD's boundary is stated in its Non-Goals.
- **Priorities** are labeled P0 (must ship) / P1 (should ship) / P2 (nice to have) on functional requirements.
- **Open decisions** are listed under a PRD's Outstanding Questions until the owner answers them.
