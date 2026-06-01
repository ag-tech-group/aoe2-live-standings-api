# aoe2-live-standings-api

Open-source live-standings API for AoE2: DE tournaments. FastAPI + Postgres on Cloud Run. The "tournament" is a first-class entity here (multi-tournament platform); the polling worker resolves its tracked profile set from the union of every tournament's roster.

## How to run things

```bash
# Tests + lint
uv run pytest                  # full suite (350+ tests, ~13s)
uv run pytest tests/routers/test_players.py -q   # one module
uv run ruff check .            # lint
uv run ruff format .           # format (auto-applies; use --check for CI-style)

# Migrations
uv run alembic revision -m "describe the change"      # generate a stub
uv run alembic upgrade <down>:head --sql              # preview SQL for the next migration
uv run alembic heads                                  # current head id

# Local dev DB is via docker-compose.yml; tests use SQLite via metadata.create_all (no migrations run).
```

CI runs `uv run pytest` + `uv run ruff check` + `uv run ruff format --check` and deploys to Cloud Run on main-branch merges (migration step `alembic upgrade head` is part of the deploy job).

## Architecture

Domain entities live in `app/models/`. Three groups:
- **Polled** (`Player`, `PlayerRating`, `Match`, `MatchPlayer`, `LiveMatchPlayer`, `LiveStream`, `Leaderboard`): the worker writes; the API reads.
- **Tournament-scoped config** (`Tournament`, `TournamentPlayer`, `TournamentOwner`, `Team`, `TeamMember`): organizer-curated; the worker never writes; survives every poll cycle.
- **Plumbing** (`IdempotencyKey`).

`TournamentPlayer` carries either a `profile_id` (a polled identity) **xor** a `name` (an announced placeholder whose `profile_id` hasn't minted yet — typically a brand-new account). The XOR is enforced by `ck_tournament_players_profile_id_xor_name`. The poller filters `profile_id IS NOT NULL`, so placeholder rows are skipped naturally.

The polling worker lives in `app/poller/`. It runs as the same Python process but on a separate Cloud Run service (`aoe2-live-standings-api-worker`) with `cpu_idle=false` so background tasks keep ticking between requests. Tasks: live matches (~15s), player stats (~30s), recent matches (~60s), Twitch live (~60s, opt-in), YouTube live (~30m, quota-bound, opt-in).

## Conventions

**Cache-Control split** (`app/cache.py` + `_TOURNAMENT_CONFIG_CACHE_CONTROL` in `app/routers/tournaments.py`):
- Polled-data reads (`/standings`, `/live`, `/players` etc.) use the **auth-aware** `apply_live_cache_control` helper — `public, s-maxage=15` for viewers, `private, no-store` for admins (cookie presence sniffed; JWT not verified). This is what keeps an admin's read-after-write fresh while viewers stay coalesced at the CDN.
- Tournament-config reads (`GET /tournaments`, `GET /tournaments/{slug}`) use the **static** `_TOURNAMENT_CONFIG_CACHE_CONTROL = "public, s-maxage=15, max-age=0, must-revalidate"`.
- Default middleware Cache-Control since #103 is `no-store`, so cacheable endpoints **must** opt in explicitly.

**Standings sort order** (per-player `/standings`): rated rows by `current_rating` DESC (NULLS LAST), then unrated polled rows by `profile_id` ASC, then placeholders by `name` ASC. Postgres's default NULLS-FIRST under DESC is the trap; always pair `.desc()` with `.nulls_last()`.

**Team standings rank on peak** (`/teams/standings`, #158): `combined_rating_sum` / `combined_rating_average` aggregate each member's lifetime `max_rating`, not `current_rating`; teams sort by that peak-based sum desc. Members within a team are ordered by `max_rating` desc (NULLS LAST). The names of the aggregate fields stay `combined_rating_*` — their meaning changed in #158; only the docstrings called it out. Same field on each member: `current_rating` and `max_rating` are both surfaced so the FE can render a current-vs-peak overlay.

**`current` magic slug** (`CURRENT_TOURNAMENT_ALIAS` in `app/routers/tournaments.py`): the literal slug `"current"` is reserved by `get_tournament` and resolves to the most-recently-started tournament (with fallback to most-recently-created). `TournamentCreate.slug` rejects it on create (422) so a real row can't shadow the alias. Use it from external probes (Cloud Monitoring uptime check on `/v1/tournaments/current/standings`, future Sentry uptime) so they survive event rollovers without an infra redeploy. **Event-specific FEs pin to their literal slug** — `/current` is for tournament-agnostic infra, not for app code that should track one specific event.

**Polymorphic URL dispatch on `/players/{lookup}`** (PATCH/DELETE only — GET `/players/{profile_id}` stays profile_id-keyed):
- Numeric lookup → looks up by `profile_id`.
- Non-numeric → looks up by `name` (placeholder).
- `RosterPlayerCreate._name_not_numeric` rejects all-digit names so the dispatch can't alias.
- Promotion (placeholder → polled identity) is a `PATCH` with `{profile_id}` in the body — atomic: `name` clears, `profile_id` sets, presentation bag carries through.

**The `presentation` bag** (a `dict` on `TournamentPlayer`):
- Opaque to the API. The FE defines the keys.
- Current convention (FE consumes): `displayName`, `flag`, `streamUrls`, `bio`. Avoid changing key shape without coordinating with hera-streamer-invitational-2026-web#152.
- The PATCH endpoint replaces the whole bag (read-modify-write). 8 KB size cap.

**Audit actions** (`AuditAction` in `app/audit.py`) are stable strings — downstream log queries pin to them. Never rename or remove a variant; only add. Targets carried in the payload: `target_user_id`, `target_profile_id`, `target_team_id`, `target_placeholder_name`, plus action-specific keys via `**extra`.

**Tournament data window**: `start_date` / `grand_finals_date` on `Tournament` bound the per-tournament `tournament_record` aggregation. A null bound is treated as open. `grand_finals_date` is a legacy name — for ladder-race events it's just the race-end bound (the old `end_date` was dropped in #76).

## Prod write recipe

The auto-mode classifier blocks fetching prod secrets (e.g. `gcloud secrets versions access latest --secret=database-url`) so the URL+password don't land in the transcript. Standard pattern for any prod SQL write:

1. Write a `/tmp/prod-<thing>.sh` shell script. The script fetches the secret in the user's shell, starts `cloud-sql-proxy` with `--token "$(gcloud auth print-access-token --account=amr@agtechgroup.solutions)"` (avoids stale ADC), wraps the SQL in `BEGIN; ... <TRAILER>;`, and toggles `TRAILER` between `ROLLBACK` (dry run) and `COMMIT` (APPLY=1).
2. Dry-run first; show the user the row counts; ask before APPLY=1.
3. Existing examples: see prior `/tmp/prod-*.sh` scripts in `feedback_*` memory.

For `tofu apply` (`infra/terraform/`): ADC is often set to the user's personal account (`amrg@pbxenergy.com`), not the agtechgroup account that owns the GCS state bucket. The fix without touching ADC: `GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token --account=amr@agtechgroup.solutions)" tofu apply -auto-approve -var "billing_account=…"`. The `billing_account` value comes from `gcloud billing projects describe aoe2-live-standings-api`.

## What not to do

- Don't `gcloud auth application-default login` or `gcloud config set` anything (global CLAUDE.md rule; mutates shared state).
- Don't write transient artifacts to the repo (scripts → `/tmp/`; debug dumps → outside the worktree).
- Don't reintroduce single-tournament assumptions (the `TRACKED_PROFILE_IDS` env var and the seed-tournament bootstrap that minted `default`-slug rows are both gone; the platform is multi-tournament since #25–#27, and a fresh deploy now errors out without an explicit tournament created via `POST /v1/tournaments`).
- Don't propose API contract changes for FE-renderable display data — extend the `presentation` bag instead. The API is deliberately tournament-agnostic (see `[[project-overview]]`).
- Don't put alerts on the `email` notification channel for new infra/capacity policies — route through the Sentry Pub/Sub channel (see `[[feedback_infra_alert_routing]]`). Uptime + budget stay on email.
