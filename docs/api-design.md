# v1 API design

Sketch of the data model, REST endpoint shape, and polling architecture for v1. Companion to [`data-sources.md`](data-sources.md) — that one documents what we *can* get from upstream; this one documents what we *expose*.

**Status:** draft for review. Decisions land in code in subsequent PRs.

## Architecture in one paragraph

A single FastAPI process runs two things side by side: an HTTP service that serves snapshots from a Postgres database, and an asyncio background worker that polls the upstream Relic backend (`aoe-api.worldsedgelink.com/community/*`, see `data-sources.md`) and writes the results to that database. Consumers read from the HTTP service only; they never touch upstream. The DB is the source of truth for what consumers see, and the upstream is the source of truth that we mirror on a cadence.

```
┌────────────────┐  poll   ┌──────────────┐  read   ┌──────────────┐
│ Relic backend  │ ──────▶ │  Postgres    │ ──────▶ │  REST API    │
│ (upstream)     │         │  (snapshot)  │         │  /v1/*       │
└────────────────┘         └──────────────┘         └──────────────┘
                              ▲                        │
                              │                        ▼
                          asyncio worker        consumer
                          inside the API        (web client)
                          process
```

## Tracked-player set

A single static list of tracked profile IDs per deployment, sourced from `TRACKED_PROFILE_IDS` (comma-separated). The polling worker reads it at startup; adding or removing a player requires a redeploy.

This is intentional for v1: it's the simplest thing that works, it removes the need for any auth surface, and a tournament host running their own deployment naturally redeploys when their roster changes. Dynamic management is a v1.x concern.

## Data model

Four entities. All write paths live in the polling worker; all read paths live in the HTTP routers. None of these have a `user_id` or `tenant_id` — single-tenant per deployment.

### Player

A tracked player profile. One row per tracked profile_id.

| Field | Type | Source / notes |
|---|---|---|
| `profile_id` | int (PK) | Relic profile ID |
| `alias` | str | `GetPersonalStat.statGroups[0].members[0].alias` |
| `country` | str | ISO-3166 alpha-2 from `members[0].country` |
| `steam_id` | str \| null | parsed from `members[0].name` (`/steam/<id>`) |
| `level` | int | `members[0].level` |
| `xp` | int | `members[0].xp` |
| `region_id` | int | `members[0].leaderboardregion_id` |
| `clan_name` | str \| null | `members[0].clanlist_name` |
| `updated_at` | datetime | wall-clock at last poll |

### PlayerRating

A player's stats on one leaderboard. One row per `(profile_id, leaderboard_id)`.

| Field | Type | Source / notes |
|---|---|---|
| `profile_id` | int (PK, FK→Player) | |
| `leaderboard_id` | int (PK) | e.g. `3` for `SOLO_RM_RANKED` |
| `current_rating` | int | `rating` |
| `max_rating` | int | `highestrating` |
| `wins` | int | |
| `losses` | int | |
| `streak` | int | positive = win streak, negative = loss streak |
| `drops` | int | |
| `rank` | int \| null | global rank (`-1` upstream → null) |
| `rank_total` | int \| null | leaderboard size |
| `region_rank` | int \| null | regional rank |
| `region_rank_total` | int \| null | regional leaderboard size |
| `last_match_at` | datetime | from `lastmatchdate` (unix → utc) |
| `updated_at` | datetime | wall-clock at last poll |

### Match

A single ranked or tournament match. One row per upstream `match_id`.

| Field | Type | Source / notes |
|---|---|---|
| `match_id` | int (PK) | upstream `id` |
| `map_name` | str | from `mapname` (raw `.rms` filename in v1; display-name mapping in v1.x) |
| `matchtype_id` | int | e.g. `6` for `SOLO_RM_RANKED 1V1` |
| `leaderboard_id` | int \| null | derived from matchtype via `getAvailableLeaderboards` cache |
| `started_at` | datetime | from `startgametime` |
| `completed_at` | datetime \| null | from `completiontime`; null means in-progress |
| `description` | str \| null | from `description` (mostly relevant for custom lobbies) |
| `state` | enum | `staging`, `in_progress`, `completed` — derived |
| `updated_at` | datetime | wall-clock at last poll |

### MatchPlayer

One row per player per match.

| Field | Type | Source / notes |
|---|---|---|
| `match_id` | int (PK, FK→Match) | |
| `profile_id` | int (PK) | (not FK — opponents need not be tracked) |
| `civilization_id` | int | |
| `team_id` | int | |
| `outcome` | enum \| null | `win`, `loss`, null while in-progress |
| `old_rating` | int \| null | `oldrating` |
| `new_rating` | int \| null | `newrating` |
| `xp_gained` | int | |

The opponents of tracked players are stored here too (no FK to Player), so a tournament page can render full match results without us having to track every Elo-2000 player in the world.

## REST endpoints

All under `/v1/`. The standing template — Pydantic models + FastAPI routers — generates OpenAPI 3 that orval reads on the consumer side, so types are free if we keep schemas explicit (no `Any`, no untyped dicts).

| Method | Path | Returns | Notes |
|---|---|---|---|
| `GET` | `/v1/players` | list of `Player` with embedded `PlayerRating[]` | Default sort: alphabetical alias. Optional `?leaderboard_id=N` to filter ratings to one leaderboard |
| `GET` | `/v1/players/{profile_id}` | `Player` + ratings + last N matches | `?match_limit=20` (default 20, max 100) |
| `GET` | `/v1/leaderboards` | list of `Leaderboard` metadata | Cached from upstream `getAvailableLeaderboards` |
| `GET` | `/v1/leaderboards/{leaderboard_id}/standings` | tracked players' ratings on that leaderboard, sorted by current rating desc | The core "live standings" endpoint |
| `GET` | `/v1/matches` | recent matches involving any tracked player | `?profile_id=N`, `?leaderboard_id=N`, `?state=staging\|in_progress\|completed`, `?limit=N` (default 50, max 200), ordered by `started_at desc` |
| `GET` | `/v1/matches/{match_id}` | full match with all `MatchPlayer` rows | |
| `GET` | `/v1/live` | matches currently in `staging` or `in_progress` involving tracked players | Short cache (`max-age=10`) |

All responses include a `last_polled_at` field at the top level so consumers can show "data freshness" indicators. Polling failures don't return errors — we serve stale data.

### Pagination

No pagination in v1. All list endpoints return at most `?limit` rows (default 50, max 200 on `/v1/matches`; bounded by the tracked-player set on standings/players/leaderboards). The tournament datasets are small enough — 50 players over a 2-week event is well under 10k matches — that "give me the most recent N" covers every consumer view, and a scrollable page beats a paginated one for the live-standings UX.

Revisit with cursor or offset pagination only if a host's deployment grows past those bounds.

### Caching

- Standings, players, leaderboards: `Cache-Control: public, max-age=15` — polling cadence is 30s so 15s is a safe shared cache.
- Match details: `Cache-Control: public, max-age=60` once `completed_at` is set; `no-store` while in-progress.
- Live: `Cache-Control: public, max-age=10`.

## Polling strategy

Three independent asyncio tasks running on `app.startup`:

| Task | Cadence | Upstream call | Writes |
|---|---|---|---|
| `poll_player_stats` | every 30s | `GetPersonalStat?profile_ids=[all_tracked]` (one call) | `Player`, `PlayerRating` upserts |
| `poll_recent_matches` | every 60s per profile, concurrency-capped at 4 | `getRecentMatchHistory?profile_ids=[N]` | `Match`, `MatchPlayer` upserts (only new match IDs) |
| `poll_live_matches` | every 15s | `findAdvertisements` (one call), filter by tracked profile_ids in `matchmembers` | `Match` state updates (`staging` → `in_progress`); no MatchPlayer until completion |

Worker tasks share an `httpx.AsyncClient` instance with a sane timeout (10s) and retry the next cycle on failure — no in-loop retry. All failures are logged with `request_id`-style correlation.

Static metadata (leaderboards, civs, regions) loaded once at startup from `getAvailableLeaderboards` and cached in memory. Refreshed on a daily cadence if needed.

## Out of scope for v1

- **WebSocket push.** Polling cadence (15–60s) is good enough for live standings. WS is a v1.x goal.
- **Avatars.** Consumers fetch from Steam Web API using `steam_id` we expose.
- **Map name → display name mapping.** Filenames like `Kawasan.rms` go to the client raw. A lookup table lands in v1.x.
- **Tournament-mode leaderboards.** Relic has these as first-class objects (`leaderboard_id` 27–30 for the upcoming Wololo events). We expose them via the standard leaderboard endpoints — no special-casing.
- **Admin auth + dynamic tracked-set management.** Env-var-only in v1; revisit if tournament hosts need it.
- **Multi-tenant.** Each host runs their own deployment with their own tracked set.

## Open questions before implementation

- **Match state derivation.** Upstream gives `state` on `findAdvertisements` (we saw all `state=0` in the probe) and `completiontime` on `getRecentMatchHistory`. What does `state` look like once a match starts? Need to observe a real match transition before the first tournament rehearsal — captured as a deferred follow-up in `data-sources.md`.
- **In-progress visibility on `getRecentMatchHistory`.** Same — does an active match appear with `completiontime=0`, or only after end? Affects whether the `poll_recent_matches` task or `poll_live_matches` task is the source of truth for "match in progress."
- **Database migrations during the spike phase.** First migration introduces all four entities at once. Alternative: ship Player + PlayerRating first (simplest read path: standings), then Match + MatchPlayer in a second migration. Probably do it all at once — they're tightly coupled.
- **Index strategy.** `Match` will be queried by `started_at desc` (matches feed) and by `state` (live feed). `PlayerRating` will be queried by `(leaderboard_id, current_rating desc)` (standings). Plan indexes for these from the first migration.

## Implementation order

1. Models + migration (one PR)
2. Schemas + read endpoints with empty DB (one PR — uses test fixtures to verify shape)
3. Polling worker (one PR — fills the DB on cadence)
4. Wire endpoints to read polled data + integration smoke test (one PR)
5. Deploy preview, dress-rehearsal with the consumer

If review surfaces a major redesign, we revise this doc before any of the above.
