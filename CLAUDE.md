@docs/conventions.md

# Calendar Deconflicter

The product is specified by the PRD in `prds/`; start with `prds/00-overview.md`. The team that builds it works by `docs/agentic_coding_team.md`. The app lives in `src/dispatcher/` and its tests in `tests/`.

## Commands

- **Install:** `uv sync`
- **Run:** `uv run python -m dispatcher <command>`
- **Test:** `uv run pytest <path>` for the tests a change touches; `bash scripts/suite.sh` for the full suite, split across one process per CPU, each with its own libSQL container (needs Docker)
- **Format and lint:** `uv run ruff format` and `uv run ruff check`

pytest and ruff are dev-only dependencies of the project.

The app and its tests need `TURSO_DATABASE_URL`. In CI it is `http://127.0.0.1:8080`, a libSQL server container that starts empty for each job and takes no auth token.

`.env.example` lists every setting the app reads. The owner's `.env` is gitignored and never reaches CI.

## Conventions

The team records the conventions it establishes here, through a PR. None are recorded yet.
