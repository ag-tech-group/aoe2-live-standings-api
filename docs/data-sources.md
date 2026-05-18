# Data sources

Working notes for the feasibility spike that gates API design. This file is a living lab notebook — updated as the investigation progresses.

**Status:** in progress.
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

| Goal                                                      | Status     | Endpoint / field                                                            |
| --------------------------------------------------------- | ---------- | --------------------------------------------------------------------------- |
| Current rating + max rating                               | ✅ proven  | `GetPersonalStat` → `leaderboardStats[*].rating` / `.highestrating`         |
| Recent matches (opponents, civs, map, outcome, Elo delta) | ✅ proven  | `getRecentMatchHistory` → `matchHistoryStats[*]`                            |
| Win/loss streak                                           | ✅ proven  | `GetPersonalStat` → `leaderboardStats[*].streak` (also per-match snapshot)  |
| Steam ID (for avatar via Steam Web API)                   | ✅ proven  | `GetPersonalStat` → `statGroups[*].members[*].name` (parse trailing path)   |
| Live match detection (player currently in ranked match)   | ❓ unknown | Investigating next — not in any of the three endpoints tested               |
| Rate limits, ToS posture                                  | ❓ unknown | Pending empirical probe                                                     |

Sample responses (uncommitted, on local disk): `/tmp/aoe2-spike/hera_personalstat.json`, `/tmp/aoe2-spike/hera_matches.json`.

## Open questions

- Live-match signal — does any reachable endpoint expose "match in progress" before the match appears in finished-match history? Could also be solved by polling `lastmatchdate` and inferring an in-progress match when wall-clock minus last-known-completion exceeds a threshold, but a direct signal would be cleaner.
- Rate limits — are there any? Header-advertised, or just behavioural? Need an empirical probe (e.g. 100 sequential calls and measure).
- Response stability — field names and shapes look directly modellable as Pydantic, but worth checking a second player and a low-Elo player to make sure all the conditional fields appear.
- ToS posture — are these endpoints documented for third-party use, tolerated, or in a grey area? No obvious published terms attached to the endpoint itself.

## Decision

_(TBD — pending probe results.)_

## Test fixtures

- Hera: profile ID `199325`
- ACCM: profile ID `347269`
