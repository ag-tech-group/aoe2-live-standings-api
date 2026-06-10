# Data sources

Working notes for the feasibility spike that gates API design. This file is a living lab notebook — updated as the investigation progresses.

**Status:** spike complete — decision below. Open follow-ups are deferred and non-blocking.
**Tracking issue:** [#1](https://github.com/ag-tech-group/aoe2-live-standings-api/issues/1)

## Goal

Confirm we can reliably read, from publicly-reachable endpoints, the data needed to power live tournament standings for AoE2: DE:

- A tracked player's current 1v1 (and team) Elo rating and max rating
- Their recent ranked match history (opponents, civs, map, outcome, timestamps)
- Detection that a tracked player has *started* a ranked match (for live "in-game" indicators)

Until this is proven, endpoint design, data model, polling cadence, and deployment decisions are all on hold.

## Method

- HTTP probing only — no in-game packet capture for v0.
- Test players: profile ID `199325` (Hera), `347269` (ACCM). Both are active high-Elo 1v1 players, so any recent-matches endpoint should have data.
- Throwaway probe scripts live in `/tmp/aoe2-spike/` (not in this repo) per the project's repo-hygiene rules.

## Candidate sources

| Source                                       | What it might cover                             | Status      |
| -------------------------------------------- | ----------------------------------------------- | ----------- |
| `aoe-api.worldsedgelink.com` (Relic backend) | Leaderboards, profiles, match history           | To probe    |
| In-game spectator / lobby endpoint           | Real-time "player started match" signal         | Investigate |
| Steam Web API                                | Profile metadata, avatars                       | To probe    |
| `ageofempires.com/stats/ageiide/` page XHRs  | Same backend as worldsedgelink, different shell | To probe    |
| Community JSON dumps (e.g. aoestats.io)      | Match history, leaderboard snapshots            | Fallback    |

## Findings

### 2026-05-18 — Initial map

- The Microsoft stats page at `https://www.ageofempires.com/stats/ageiide/` is a WordPress page that loads a single bundled JS file (`main.3d9f5f.js` at the time of probe). API URLs and request shapes live in that bundle — the HTML alone is not informative.
- Community references (forum threads, third-party wrappers) confirm `aoe-api.worldsedgelink.com` as the Relic / World's Edge backend. The `/community/*` endpoints are reportedly accessible without a platform login since the AoE-platform-login dependency was removed.
- The historical `aoe2.net` community API is sunset and not usable.

### 2026-05-18 — Primary source confirmed: `aoe-api.worldsedgelink.com`

The Relic / World's Edge community backend returns rich JSON on simple GET requests with no auth, no API key, no cookies. Probed live with Hera (`profile_id=199325`):

**Working endpoints**

- `GET /community/leaderboard/getAvailableLeaderboards?title=age2`
  Full metadata: leaderboard IDs, match-type IDs (1v1/2v2/3v3/4v4 per game mode), 60+ civilization IDs, regions. Includes 18 ranked-ladder leaderboards (1v1 RM/EW/DM, Team RM/EW/DM, BR, Custom POM) plus four already wired for the Red Bull Wololo tournament — tournament-specific leaderboards are first-class objects in this API.

- `GET /community/leaderboard/GetPersonalStat?title=age2&profile_ids=[199325]`
  Returns:
  - `statGroups[0].members[0]` — `profile_id`, `alias`, `name` (Steam URL containing Steam ID), `country`, `level`, `xp`, `leaderboardregion_id`, `clanlist_name`
  - `leaderboardStats[]` — one row per leaderboard the player has touched, with `leaderboard_id`, `rating` (current Elo), `highestrating` (peak), `wins`, `losses`, `streak`, `drops`, `rank`, `ranktotal`, `regionrank`, `regionranktotal`, `lastmatchdate` (unix epoch seconds)

- `GET /community/leaderboard/getRecentMatchHistory?title=age2&profile_ids=[199325]`
  Up to N recent finished matches. Each match carries `id`, `mapname`, `matchtype_id`, `startgametime`, `completiontime`, plus two per-player arrays: `matchhistoryreportresults` (outcome, `civilization_id`, `xpgained`) and `matchhistorymember` (`oldrating` → `newrating`, snapshot of `wins`/`losses`/`streak` at the time of the match).

**Auth posture:** none observed. No login token, no API key, no rate-limit headers in tested responses. Rate-limit cadence still needs empirical probing.

**Coverage vs. spike goals**

| Goal                                                      | Status    | Endpoint / field                                                                       |
| --------------------------------------------------------- | --------- | -------------------------------------------------------------------------------------- |
| Current rating + max rating                               | ✅ proven | `GetPersonalStat` → `leaderboardStats[*].rating` / `.highestrating`                    |
| Recent matches (opponents, civs, map, outcome, Elo delta) | ✅ proven | `getRecentMatchHistory` → `matchHistoryStats[*]`                                       |
| Win/loss streak                                           | ✅ proven | `GetPersonalStat` → `leaderboardStats[*].streak` (also per-match snapshot)             |
| Steam ID (for avatar via Steam Web API)                   | ✅ proven | `GetPersonalStat` → `statGroups[*].members[*].name` (parse trailing path)              |
| Live tournament-mode match detection (custom lobby)       | ✅ proven | `findAdvertisements` (see below) — tournament hosts use custom lobbies                 |
| Live ranked auto-match detection                          | ⚙ via polling | No real-time push found; poll `getRecentMatchHistory` at 30–60s per tracked player |
| Rate limits                                               | ✅ benign | 30 sequential calls at full speed: all 200, p95 307ms, no headers, no 429s             |
| Auth requirements                                         | ✅ none   | `/community/*` open; `/game/*` returns 401 (login required, not needed for v1)         |

### 2026-05-18 — Schema stability + batch query support

- `GetPersonalStat` shape verified identical between Hera (`199325`) and ACCM (`347269`).
- **Batch queries supported:** passing `profile_ids=[199325,347269]` returns both players' `statGroups` and `leaderboardStats` in a single call. Same support on `getRecentMatchHistory`. Implication: for ~32 tracked tournament players we can fetch current ratings in 1–2 HTTP calls instead of fanning out per-player.

### 2026-05-18 — Live match detection

- `GET /community/advertisement/findAdvertisements?title=age2` returns the current open-lobby list. At time of probe: 88 lobbies, all `state=0` (staging), mostly `matchtype_id=0` (custom). The response also carries an `avatars` array (misnamed — it's actually a full profile dictionary for every player in those lobbies: profile_id, alias, country, level, xp, region).
- **Tournament-mode live detection** is well-served by polling this endpoint and matching `matchmembers[*].profile_id` against the tracked-player set. Tournament hosts overwhelmingly use custom lobbies (observer slots, fixed map pools, password-locked rooms).
- **Ranked auto-match live detection** does *not* appear on this surface (Hera was not in the snapshot, and ranked queueing is matched server-side without a public lobby). Workable v1 approach: poll `getRecentMatchHistory` per tracked profile at 30–60s cadence; the appearance of a new `id` indicates a match started. Whether in-progress matches surface here with `completiontime=0` was not testable without a live target — needs validation against a player who is actively mid-match.
- `getMatchHistory` (no qualifier) exists but returns 400 to every parameter shape tried (matchID, profileID, profile_id, aliases, steamID). Not investigated further; `getRecentMatchHistory` covers the use case.
- A separate `/game/advertisement/findAdvertisements` endpoint exists at the same host but returns 401 — that surface is platform-login-gated and not relevant to a server-side polling consumer.

### 2026-05-18 — Rate-limit empirical probe

- 30 sequential `GetPersonalStat` calls from a single IP with no inter-call delay: 30/30 returned 200, identical payload sizes, no rate-limit headers in any response, p50 latency 286ms, p95 307ms, max 346ms.
- At the project's planned scale (~16–32 tracked players, batched into 1–2 calls every 30s, plus one `findAdvertisements` call every ~15s) we are at <0.2 RPS — orders of magnitude under any plausible limit. Higher-volume testing is unnecessary for v1.

Sample responses (uncommitted, on local disk): `/tmp/aoe2-spike/{hera,accm,batch}_personalstat.json`, `/tmp/aoe2-spike/hera_matches.json`, `/tmp/aoe2-spike/live_advertisements.json`.

### 2026-06-10 — `mapname` is unreliable; real map lives in the `options` blob (#265)

- `getRecentMatchHistory`'s `mapname` field is wrong for roughly half of ranked automatch games (verified against replay-derived ground truth: 10 of 20 sampled matches mislabeled, e.g. a Black Forest game reported as `Marketplace.rms`). The value never self-corrects.
- The authoritative map travels in the match's `options` field: `base64(zlib(JSON string))`, where the JSON string is base64 of `[u8 record_count][record_count × (u32 length, ASCII "key:value")]`. Key `10` is the **locstring id** of the map's display name (`10875` = Arabia, `10878` = Black Forest, …; `301xxx` = DLC-shipped pool maps). Key `10` = `0` means a custom RMS file was hosted and `-2` a scenario — for those, `mapname` *is* the hosted file name and is trustworthy. Pre-automatch2 matches (years old) carry the legacy `11` key instead; no map id is recoverable there.
- The poller decodes the blob in `app/poller/map_names.py` and resolves the name through a verified-only locstring table (each entry cross-checked against replay-derived data); unknown ids fall back to raw `mapname` and log `unknown_map_locstring` once per process. Extend the table by decoding key 10 for a few matches on the new map and confirming the name against a replay-derived source (e.g. aoe2insights match pages).
- `slotinfo` (same wrapping) carries per-slot civ/team data but no map. `matchurls` exposes per-player replay downloads (~750 KB gz) — the fully authoritative source, deliberately not used (heavy, needs a replay parser that breaks on game patches).

### 2026-06-10 — `findAdvertisements` response shape (#267)

- The open-lobby list is the top-level **`matches`** key (not `advertisements`), and each lobby's id is **`id`** — `match_id` only appears inside the `matchmembers` entries. There is no `creation_time` field. The shape the live parser originally coded against (`advertisements` + per-lobby `match_id` + `creation_time`) never matched a payload we can reproduce; whether upstream renamed it post-spike or the original fixture was mistranscribed is unknowable (no capture survived). Either way the parser produced zero live rows until #267.
- Lobby `mapname` is usually the placeholder `"my map"` even when the lobby hosts a standard map — the `options` blob (same format as on match history, see the #265 entry above) carries the real map locstring.
- All 90 lobbies in the fresh capture were `state=0, visible=1`, `matchtype_id=0` (customs). Ranked automatch still does not appear on this surface, and `state` transitions remain unobserved — the staging→in-progress inference from the spike stands unvalidated.

## Open questions (deferred, not blocking)

- **`state` field transitions on `findAdvertisements`.** All 88 lobbies in the snapshot were `state=0`. State likely advances (staging → loading → playing → finished) as the match progresses, but the transition needs to be observed live. Validate during the first tournament dress-rehearsal.
- **In-progress match visibility on `getRecentMatchHistory`.** Does a match appear here with `completiontime=0` (or absent) while still ongoing, or only after it ends? Test against a player who is actively mid-match.
- **ToS posture.** No published terms attached to the `aoe-api.worldsedgelink.com/community/*` endpoints; community usage is widespread (third-party wrappers, community competitor sites). Low risk but worth a polite outreach to World's Edge before public launch to confirm we won't surprise them.
- **Low-Elo / never-played-ranked profile shape.** Verified shape parity between two top-15 players. Worth one more spot-check against a brand-new account or a 1000-Elo player to make sure conditional fields don't disappear or change types.

## Decision

**Go.** The data-source feasibility question is answered favourably.

**Primary source:** `aoe-api.worldsedgelink.com/community/*` — open (no auth), stable schema, batch query support, benign rate limits at planned scale.

**Endpoints in v1:**

| Need                                         | Endpoint                                          | Cadence                                |
| -------------------------------------------- | ------------------------------------------------- | -------------------------------------- |
| Current rating, max rating, streak, country  | `GetPersonalStat`                                 | Every 30s (one call, batched profiles) |
| Recent matches, Elo deltas, civs, outcomes   | `getRecentMatchHistory`                           | Every 30–60s per tracked profile       |
| Live custom-lobby / tournament-mode matches  | `findAdvertisements`                              | Every 15s                              |
| Static metadata (leaderboards, civs, regions, match types) | `getAvailableLeaderboards`            | Once at startup, cache                 |
| Avatar images                                | Steam Web API, keyed by Steam ID from `members[*].name` | Once per player, cache              |

**Fallback:** None hardened for v1. If the primary source becomes unreliable, options are aoestats.io's community API (independent surface, different shape) or reactive incident response. Treat as accepted risk for a v0.1 launch.

**Out of scope for the spike (deferred):** the four open questions above — all answerable during tournament rehearsals / live operation, none blocking API design.

**What's unblocked:** endpoint design (Pydantic schemas, REST shape), data model (players, matches, ratings, leaderboards), polling worker design, deployment planning.

## Test fixtures

- Hera: profile ID `199325`
- ACCM: profile ID `347269`
