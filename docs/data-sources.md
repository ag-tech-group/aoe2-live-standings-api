# Data sources

Working notes for the feasibility spike that gates API design. This file is a living lab notebook — updated as the investigation progresses.

**Status:** in progress.
**Tracking issue:** _(to be filled in once opened)_

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
- No findings yet on actual endpoint paths, response shapes, or rate limits — probing next.

## Open questions

- Exact endpoint paths and query parameters on `aoe-api.worldsedgelink.com`.
- Response stability — are field names and shapes consistent enough to model as Pydantic schemas, or do we need a translation layer?
- Rate limits — are there any? Header-advertised, or just behavioural?
- Live-match signal — does any reachable endpoint expose "match in progress" before the match appears in finished-match history?
- ToS posture — are these endpoints documented for third-party use, tolerated, or in a grey area?

## Decision

_(TBD — pending probe results.)_

## Test fixtures

- Hera: profile ID `199325`
- ACCM: profile ID `347269`
