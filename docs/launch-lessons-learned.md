# Launch Lessons Learned — Live-Event Hardening

> **Purpose:** capture the failures, root causes, and fixes from running this
> service under real live-event load, written so a *future* project (ours or
> anyone's) can avoid the same traps. Incident-anchored to the 2026-06-01
> King's Gauntlet launch, but the lessons are meant to generalize.
>
> **Status: PARTIAL.** Frontend / observability (Sentry) incidents are written
> up below; the API / infra, worker, and edge incidents are written up below as well.

<!-- How to use: for each item in "Incidents & fixes" below, copy the
     "Per-incident template" and fill it in. Promote the durable, reusable
     takeaways up into "Generalizable lessons / pre-launch checklist". -->

---

## Metadata

- **Event / context:** The King's Gauntlet — Hera's AoE2:DE 1v1 invitational; first live event. Real-time public standings: a code-split SPA frontend over one shared REST + SSE backend (this service).
- **Date(s):** 2026-06-01 (public launch / launch day).
- **Authors:** _(fill in)_
- **Severity / impact:** Frontend: mostly transient, viewer-facing degradation that self-recovered (chart tooltips, lazy-route loads). One hard outage: a Cloudflare Worker (the `criticalbit-router` edge) hit a limit → full site down ~17:31–17:38 UTC (~7 min). API side: two same-day full outages of standings *data* — the morning connection-exhaustion outage (all endpoints 5xx) and the afternoon 429 storm (~80% of viewer reads rejected); both resolved same-day, no data loss.

---

## TL;DR

**API / infra:** Launch day hit two same-day, viewer-facing outages of the standings *data* — a morning connection-exhaustion outage (stale Cloud Run revisions saturated Cloud SQL's `max_connections`; #171 prune + #172 pool) and an afternoon 429 storm (the limiter keyed on Cloudflare's shared edge IP, and `Vary: Cookie` defeated CDN coalescing; #176). The 429 fix was itself blocked by a connection-exhaustion **deploy deadlock** until a Cloud SQL tier bump (#173) raised the ceiling. Separately, the #167 `team_members` re-key landed mid-event with **zero downtime** via expand → transition (#179) → contract (#181), plus read-key exposure (#184) and progression windowing (#185). Full write-ups in "API / infra incident write-ups" below.

**Frontend / observability:** Launch-day Sentry surfaced four classes of frontend error — stale code-split chunks after each redeploy, an ECharts hover crash during live data updates, `AbortError`s from SSE-nudge fetch cancellation, and browser-extension noise. Most were transient and self-recovered. The fixes: auto-reload on stale-chunk loads, merge chart updates in place instead of rebuilding, and suppress intentional-cancellation / teardown noise at Sentry's `beforeSend`. Net frontend code impact was low; the durable wins are the patterns in "Generalizable lessons" below.

---

## Timeline

_Times UTC, 2026-06-01._

**Frontend / observability:**

- Through launch: stale-chunk dynamic-import errors, an ECharts `getRawIndex` hover crash, and SSE-nudge `AbortError`s trickle in as viewers load the live site across successive deploys.
- **~17:14** — frontend fixes deployed (release `f147e03`): ECharts in-place merge (FE #260), `vite:preloadError` auto-reload (FE #261), stale-chunk teardown `beforeSend` (FE #263), SSE-abort call-site catch (FE #265).
- **17:23** — an `AbortError` (`-9`) fires on the *fixed* build under normal load → the #265 catch was incomplete (it caught the wrong promise).
- **17:31–17:38** — Cloudflare Worker (`criticalbit-router`) hits a limit; full site outage (~7 min).
- **~18:1x** — durable `AbortError` suppression deployed (FE #267, `beforeSend` drops `AbortError`).
- **after ~17:14** — API spec drift (#166: team-member `alias` / `current_rating` made nullable) lands, turning the frontend's `verify-api-types` CI red.

**API / infra (UTC):**

- Morning — stale Cloud Run revisions saturate Cloud SQL `max_connections=100` → all endpoints 5xx. Fixed by the CI prune step (#171) + a tighter per-instance pool (#172), merged + deployed ~18:30–18:47.
- Event window — 429 storm: ~80% of viewer reads rejected (limiter on the CF edge IP + `Vary: Cookie` cache fragmentation).
- #176 (the 429 fix) blocked ~4× by `TooManyConnectionsError` at the `migrate` step — the connection-exhaustion **deploy deadlock**.
- **~20:00** — applied #173 (`tofu apply`: Cloud SQL → `db-custom-1-3840`, `max_connections 200`, ~5-min restart); re-ran the deploy; #176 landed; 429s → 0.
- **~20:10–20:40** — #167 re-key shipped zero-downtime: #179 (transition), #181 (contract), #184 (expose `tournament_player_id` on reads).
- **~21:25** — #185 windowed `/progression` to the event dates (the stats chart had shown data back to 2021).

---

## Incidents & fixes to document

> Stub list of what we actually hit during the launch — expand each one below
> using the per-incident template. (Titles only here so nothing gets lost.)

- [x] Cloud Run stale-revision accumulation saturated Cloud SQL connections _(first outage; #171 prune, #172 pool)_
- [x] DB connection-pool sizing vs Cloud Run autoscaling (per-instance pool × maxScale)
- [x] Rate limiter keyed on Cloudflare's edge IP instead of the real client IP → 429 storm _(#176)_
- [x] `Vary: Cookie` fragmented the CDN cache → no viewer coalescing → origin flood _(#176)_
- [x] CDN didn't cache JSON by default — missing Cloudflare Cache Rule _(#178, follow-up)_
- [x] Connection exhaustion blocked the very deploy meant to fix it (deadlock) → broke it via a Cloud SQL tier bump / manual revision prune _(#173)_
- [x] Cloud SQL tier / `max_connections` headroom for live-event scale _(#173)_
- [x] Destructive migrations during Cloud Run rollover (expand-then-contract discipline) _(#179 transition, #181 contract)_
- [x] CI deploy pipeline: a failing `migrate` step silently skips deploy + prune
- [x] Worker service stuck serving an old revision (deploys not rolling forward) _(root cause: worker startup blocked on the DB — see #177 write-up)_

  _Frontend / observability (Sentry — written up below):_
- [x] Stale code-split chunks 404 after redeploy → dynamic-import + router-teardown errors _(FE #261 auto-reload, #263 teardown suppression)_
- [x] ECharts live-update hover crash — `notMerge` rebuild races the hover handler _(FE #260)_
- [x] `AbortError` from SSE-nudge fetch cancellation — `void` ≠ handled; leaked from the cancelled fetch's own promise _(FE #265 → #267)_
- [x] Browser-extension noise (`Maximum call stack` from a monkey-patched native) — ignored, not our code
- [x] Sentry resolution hygiene — plain-resolve regresses on any event; git-SHA releases don't order for resolve-in-next-release
- [ ] _(add any others)_

---

## Frontend incident write-ups (Sentry)

### Stale code-split chunks after redeploy

- **Symptom (what we saw):** `TypeError: Failed to fetch dynamically imported module` (Chrome) / `error loading dynamically imported module` (Firefox) for `stats.lazy-<hash>.js`, plus teardown `TypeError: …reading 'component' / 'options'` from the router. Sentry `HERA-…-1 / -4 / -5 / -6 / -8`.
- **Impact:** a viewer whose tab is on a *previous* build, navigating to a lazy route (e.g. `/stats`), gets a broken view until they refresh. Transient; recurs at *every* deploy — and we deployed several times on launch day.
- **Root cause:** each build content-hashes chunk filenames; an old tab's route table points at a chunk the new deploy has already replaced, so `import()` 404s.
- **Why it wasn't caught earlier:** invisible in dev and single-deploy testing; only shows when a deploy lands under live traffic with already-open tabs.
- **Fix:** FE #261 — listen for Vite's `vite:preloadError`, `preventDefault()` + one guarded `location.reload()` to pick up the current build (with a sessionStorage guard so a genuinely broken deploy doesn't reload-loop). FE #263 — `beforeSend` drops the teardown error (under `preventDefault` the failed import resolves `undefined`, so the router throws on it in the microtask before the reload navigates away).
- **Detect faster:** alert on dynamic-import errors spiking immediately after a deploy (a release-health signal).
- **Lesson for future projects:** any code-split SPA orphans chunks for in-flight tabs on *every* deploy — ship auto-reload-on-chunk-error from day one, and expect a per-deploy blip during a live event (consider a deploy freeze at peak).

### ECharts live-update hover crash

- **Symptom:** `TypeError: …reading 'getRawIndex'` on chart hover. Sentry `HERA-…-3`.
- **Impact:** a failed tooltip on the exact frame a data refresh lands while the cursor is over the chart. Uncaught (fires from ECharts' own DOM `mousemove` listener, outside React's error boundary) but the page survives.
- **Root cause:** charts rendered with `notMerge`, so every SSE-driven refetch tore down and rebuilt the series model; a `mousemove` landing mid-rebuild read `getDataParams` off a just-disposed model.
- **Fix:** FE #260 — drop `notMerge`, give each series a stable `id` so ECharts merges in place; plus a `useStableValue` hook so value-identical polls don't touch the chart at all. (Bonus: merge preserves the dataZoom window + legend selection across updates, which `notMerge` reset each poll.)
- **Lesson for future projects:** live-updating charts must **merge in place**, never rebuild — a destructive re-render races the charting library's async internal/event state.

### AbortError from SSE-nudge fetch cancellation

- **Symptom:** `AbortError: Fetch is aborted` / `AbortError: signal is aborted without reason`, unhandled rejection. Sentry `HERA-…-7 / -9`.
- **Impact:** none functional — pure noise — but it kept reappearing across browsers/releases.
- **Root cause:** a burst of SSE nudges invalidates a query whose refetch is still in flight; React Query aborts the in-flight request. The rejection leaks from the **cancelled fetch's own promise** (owned by the component's `useQuery`), not the `invalidateQueries` promise.
- **Why it wasn't caught earlier:** the first fix (FE #265) `.catch()`-ed the `invalidateQueries` promise — the wrong one — and *looked* fixed; `-9` then fired on the fixed build (8 min before an unrelated outage) and proved it incomplete.
- **Fix:** FE #267 — drop any `AbortError` at Sentry's `beforeSend`. Intentional cancellation is never actionable; suppress at the reporting boundary rather than chase every promise that might own a cancelled fetch.
- **Lesson for future projects:** `void promise` silences the floating-promise lint, not the runtime rejection. For "expected noise" classes (AbortError, extension errors), filter at `beforeSend`. And **verify a fix against an event from the fixed build** before trusting it.

### Browser-extension noise (not our code)

- **Symptom:** `RangeError: Maximum call stack size exceeded`, every frame `<anonymous>`, a monkey-patched `Object.getOwnPropertyDescriptor` recursing into itself. Sentry `HERA-…-2`.
- **Root cause:** a browser extension on one viewer's machine recursively wrapped a native method. Not our code — our prod build ships (hidden) sourcemaps, so real frames would be symbolicated; all-`<anonymous>` means injected/eval'd script.
- **Fix:** ignored in Sentry.
- **Lesson for future projects:** all-`<anonymous>`/eval frames + a monkey-patched native = third-party (extension) noise; ignore, don't chase. Note Sentry's built-in "browser extensions" inbound filter misses these because the frames aren't `chrome-extension://` URLs.

---

## Per-incident template

> Copy this block per incident above.

### <incident title>

- **Symptom (what we saw):** _(fill in)_
- **Impact (who/what, how long):** _(fill in)_
- **Root cause:** _(fill in)_
- **Why it wasn't caught earlier:** _(fill in)_
- **Fix (PR / commit / config change):** _(fill in)_
- **How we detected it / how we'd detect it faster next time:** _(fill in)_
- **Lesson for future projects:** _(fill in)_

---

## Generalizable lessons / pre-launch checklist

> The durable, project-agnostic takeaways — the part future projects should
> read before their own launch.

- [ ] **Code-split SPA + frequent deploys = stale-chunk errors.** Ship auto-reload-on-chunk-load-failure (`vite:preloadError` → guarded `location.reload()`) before launch. Consider a deploy freeze during peak viewership.
- [ ] **Live-updating UI must update in place, not rebuild.** Destructive re-renders (e.g. ECharts `notMerge`) race async event handlers and library-internal state; merge by stable id instead.
- [ ] **`void promise` ≠ handled.** A fire-and-forget rejection still surfaces as an unhandled rejection, and the leaking promise may not be the one you `.catch()`.
- [ ] **Filter intentional / known noise at the reporting boundary.** `AbortError` (cancellation) and browser-extension errors are never actionable — drop them in `beforeSend`, don't chase call sites.
- [ ] **Suppressing an error can change its shape.** `preventDefault()` turned a rejected import into a resolve-`undefined`, spawning a *new* error fingerprint downstream. After any suppression, watch for the new mask.
- [ ] **Verify a fix against an event from the fixed build.** Green CI + a plausible diff isn't proof; the release tag on a recurring event tells you old-tab-tail vs genuine-regression.
- [ ] **Correlate timestamps + release tags before blaming an outage.** A "phantom" can be a real bug that merely coincided — or, as with our `-9`, one that fired *before* the outage window and was real.
- [ ] **A hard edge / Worker outage emits no new-page-load errors** (no JS loads) — only already-loaded tabs report failed-fetch / aborted errors. An outage's observability signature is *not* a broad new-issue spike.
- [ ] **Sentry resolution hygiene:** a plain "resolve" regresses on *any* new event; resolve *in-release*. Git-SHA releases (vs semver) don't order for "resolve in next release", so use **ignore / until-escalating** for benign old-tab tails.
- [ ] **API schema changes during a live event need coordination.** The API making fields nullable (#166) mid-event broke the frontend's `verify-api-types` and needs frontend null-handling — the FE is in lockstep via `generate-api`. Land schema changes with the consumer, not ahead of it.

---

## Action items / follow-ups

| Item | Owner | Status | Link |
|------|-------|--------|------|
| Regenerate frontend API types + add null-handling for now-nullable team-member `alias` / `current_rating` (#166) | frontend | TODO | FE `chore(api)` |
| Review `criticalbit-router` Cloudflare Worker request/CPU limits (caused the ~7-min outage) | infra | **DONE** — root cause was the Workers Free 100k-req/day cap (Cloudflare err 1027); upgraded to Workers Paid, collapsed the `/relay` double-proxy, added a usage alert (full write-up at the bottom of this doc) | [#9](https://github.com/ag-tech-group/criticalbit-router/issues/9) · [#10](https://github.com/ag-tech-group/criticalbit-router/issues/10) · [#11](https://github.com/ag-tech-group/criticalbit-router/pull/11) |
| Wire `criticalbit-router` to Cloudflare Workers Builds (auto-deploy on merge — closes the `merge ≠ deploy` gap that delayed the fix going live) | infra | TODO | `criticalbit-router` |

---

## References

- Frontend Sentry fixes: FE PRs **#260** (ECharts in-place merge), **#261** (chunk-reload auto-reload), **#263** (teardown `beforeSend`), **#265** (SSE-abort call-site catch), **#267** (`AbortError` `beforeSend`).
- Sentry: org `critical-bit`, project `hera-streamer-invitational-2026`. Issues `HERA-STREAMER-INVITATIONAL-2026-1` … `-9`.
- Frontend memory notes: ECharts-merge rule, stale-chunk-reload, Sentry `beforeSend` drops.

---

<!-- Appended 2026-06-01 by the stats/worker session. Two incidents not yet in
     the stub checklist above, written up inline; promote into the structure
     above as needed. -->

### Empty upstream `matchtypes` -> every match written `leaderboard_id = NULL` -> tournament standings all zeros

- **Symptom (what we saw):** Per-player standings showed 0's and dashes for every player's *tournament* record (games, W-L, streak, peak) and empty recent-results, while lifetime ladder ratings updated fine. Looked like a frontend bug; it was not.
- **Impact:** Viewer-facing tournament standings blank/zeroed for the whole roster during the live event, until #175 deployed + a data backfill. Lifetime ratings unaffected (separate poll).
- **Root cause:** `Match.leaderboard_id` is denormalized at write time (in `parse_recent_matches`) from a `matchtype_id -> leaderboard_id` map built **once at worker startup** from upstream `getAvailableLeaderboards`. That payload returned 17 leaderboards but with **empty `matchtypes` arrays**, so the map came back empty, `.get(6)` returned `None`, and every match was written `leaderboard_id = NULL`. The tournament-scoped queries filter on `leaderboard_id`, so they matched nothing -> zeroed records. The leaderboards *table* still populated (names intact), so `/v1/leaderboards` looked healthy and nothing alerted.
- **Why it wasn't caught earlier:** a once-at-startup, soft-failing dependency with no fallback and no signal when empty; tests only fed well-formed payloads (with matchtypes); and the failure surfaced as "zeros," which reads as "no games yet," not "broken."
- **Fix (PR):** #175 — a static `DEFAULT_MATCHTYPE_TO_LEADERBOARD = {6: 3}` floor merged *over* the upstream map (upstream still wins and extends when healthy), returned even on hard failure, plus a `load_leaderboards_no_matchtypes` warning so an empty payload is loud. Mistagged rows fixed with a one-time idempotent backfill (`UPDATE matches SET leaderboard_id=3 WHERE matchtype_id=6 AND leaderboard_id IS NULL`, 562 rows).
- **Detect faster next time:** alert on `load_leaderboards_no_matchtypes`; dashboard/alert on the rate of `matches` rows written with `leaderboard_id IS NULL`; a synthetic check that the current tournament's standings have non-zero aggregate games once its window opens.
- **Lesson for future projects:** never let a correctness-critical *denormalized* column depend on a single, soft-failing, once-loaded upstream field with no fallback. Encode a static floor for the stable/critical mappings, merge upstream over it, and emit a loud signal when upstream returns empty. "Empty" is a failure mode, not just "no data."

### Worker startup blocked on the DB -> the fix couldn't deploy *during* the DB incident

- **Symptom:** Worker deploy failed repeatedly with `asyncpg ... TooManyConnectionsError` -> "Application startup failed" -> Cloud Run health-check timeout; Cloud Run kept the old (broken) revision serving.
- **Root cause:** the worker lifespan `await`s `load_leaderboards` (a DB call) **before** uvicorn binds `PORT`, so a saturated/unavailable DB at startup means the container never becomes ready. The API — which starts its LISTEN/NOTIFY listener as a *background* task — deployed fine throughout the same saturation.
- **Fix (PR):** #177 — bind the port first, run `load_leaderboards` as a background task with retry; pollers start immediately on the static floor map (#175) so matches tag correctly even before the first successful load.
- **Lesson for future projects:** startup readiness must not block on a remote dependency you might need to *redeploy through*. Bind the port, then do the dependency work in the background with retry — otherwise, mid-incident, the deploy that fixes the problem can't reach the broken component.

### Follow-up (#182): a graceful low-level fallback can hide the signal the caller needs

Refining #177's background leaderboards loader: it fast-retried every 60s while upstream *persistently* returned no `matchtypes`, because `load_leaderboards` returned the static floor on **both** a fetch failure (transient — retry soon) and a healthy-but-empty load (persistent — retrying won't help). Returning a usable fallback at the low level erased the distinction the retry loop needed, so it churned (re-logging `load_leaderboards_no_matchtypes` + re-upserting every minute). Benign because the floor kept tagging, but noisy.

- **Fix:** make the failure *raise* (the floor is seeded by lifespan and kept on exception), so the loop's rule is simply success → slow refresh, exception → fast retry. Empty-matchtypes then settles into the slow refresh — one warning per refresh, not per minute.
- **Lesson for future projects:** a graceful fallback at a low layer can swallow the very signal an orchestration layer needs for a retry/backoff decision. Don't let "return something usable" collapse "it failed" into "it succeeded" — surface the failure (raise, or return a status) and let the caller own the fallback *and* the cadence.

---

<!-- Appended 2026-06-01 by the ratelimit/edge session. Full write-up of the
     criticalbit-router Worker outage (flagged in the Timeline + Action items
     above as TODO) plus the deploy/auth operational lessons from fixing it.
     Promote the generalizable bullets into "Generalizable lessons" above as
     needed. -->

## Edge incident write-up (criticalbit-router Cloudflare Worker)

### Cloudflare error 1027 — front-end zone down at kickoff (Workers Free daily request cap)

- **Symptom (what we saw):** the entire `aoe2.criticalbit.gg` zone returned **HTTP 429 with body `error code: 1027`** ("this website has been temporarily rate limited") on *every* path — `/`, `/kings-gauntlet`, even `/favicon.ico` — served straight from Cloudflare's edge (only edge headers, none of the origin's). The API subdomain (`aoe2-live-standings-api.criticalbit.gg`) stayed healthy (200), so standings *data* was fine; the site *shell* was simply unreachable. ~17:31–17:38 UTC (~7 min).
- **Impact:** full front-end outage at event kickoff — new page loads got Cloudflare's 1027 page; only already-open tabs kept working. Because no JS loads during an edge outage, it produced **no new front-end Sentry issues** — its signature was the *absence* of new-page traffic, not an error spike (this is the generalizable lesson already noted above: "A hard edge / Worker outage emits no new-page-load errors").
- **Root cause:** `aoe2.criticalbit.gg` is a static Vite SPA on **Netlify**, fronted by the **`criticalbit-router` Cloudflare Worker** bound to `aoe2.criticalbit.gg/*`. Because the Worker is on `/*`, **every** request to the host — HTML, every JS/CSS chunk, fonts, images, and the PostHog `/relay/*` analytics beacons — is **one Worker invocation**. Event-day traffic crossed the **Workers Free plan's 100,000-requests/day cap**, and Cloudflare 1027'd the whole zone until the UTC reset. (Aside, worth recording: the `criticalbit-router` Worker and the `criticalbit.gg` zone live in a **personal Cloudflare account, `amrtgaber@gmail.com`** — *not* the agtechgroup account that owns the GCP project.)
- **Why it wasn't caught earlier:** the pre-event cost model ([event-traffic-cost-model.md](./event-traffic-cost-model.md)) sized the API / DB / Netlify layers thoroughly, but didn't account for the edge Worker (added later for the #167 base-path cutover) sitting in the hot path of 100% of front-end requests behind a hard daily cap. Immutable asset caching — which the FE *does* have — cuts *origin* fetches and *browser* re-requests but **not Worker invocations**: the Worker runs before cache on its route, so even a cache hit costs an invocation. And the numeric code disambiguates the cause: **1027 ≠ 1015** — a 1015 would be a rate-limit *rule* we configured; 1027 is the account/plan request cap, a different mechanism with a different fix.
- **Fix:** immediate — **upgraded the Worker to the Workers Paid plan** (no daily cap; 10M req/mo included, then ~$0.30/M); the zone recovered at once. Then two durable follow-ups in `criticalbit-router`:
  - **Collapsed the PostHog `/relay/*` double-proxy** (#11, deployed version `a241ff2b`). Analytics had been proxied **twice** — browser → Worker → Netlify (`netlify.toml` rewrite) → PostHog — because the Worker had no `/relay` route and fell through to the Netlify origin, which re-proxied it. The Worker now proxies `/relay/*` straight to PostHog (`/relay/static/*` → assets host, `/relay/*` → ingestion host) in one hop. The now-dead Netlify rewrites were removed in the FE (hera-streamer-invitational-2026-web#270).
  - **Added a Workers usage alert** (#9): a Cloudflare *Usage-Based Billing → Workers Standard Requests* notification at 15M req/mo → email, so a future surge warns before it bites. The outage was **silent** — nothing alerted as the counter climbed to 100k.
- **How we'd detect / verify faster:** the usage alert above; and the **`x-nf-request-id` response header** is a clean probe for which layer serves `/relay` — present = still proxied through Netlify, absent = the Worker hits PostHog directly. We used it to confirm the deploy flipped (relay lost the header while the SPA kept it).
- **Lesson for future projects:** **an edge layer bound to `/*` makes request *count* the binding constraint — not bandwidth, not cache-hit-rate.** Know the platform's hard caps (Workers Free = 100k/day), keep the highest-volume *non-cacheable* traffic (here, analytics beacons) *off* the hot edge path, and alert on request volume / spend before launch. Keeping the API on a subdomain that **bypasses** the front-end edge is what kept standings data up while the shell was down — preserve that separation.

## Operational lessons from the fix (deploy & auth)

- **`merge ≠ deploy` for the edge Worker.** `criticalbit-router`'s CI only lints (`tsc --noEmit`); there's no deploy job, so merging the relay fix to `main` did **not** ship it — it sat merged-but-undeployed until a manual `wrangler deploy`. (Contrast this API repo, whose CI *does* deploy on merge.) The fix going live was delayed purely by this gap. **Follow-up:** wire `criticalbit-router` to Cloudflare Workers Builds (git-connected auto-deploy on merge).
- **OAuth-callback logins don't complete in the headless / WSL shell.** Both `wrangler login` and the Cloudflare MCP OAuth redirect to a `localhost:<port>` callback the browser can't reach back into this environment, so the CLI never captures the token (`wrangler login` left the credential file untouched; deploys kept failing with `/memberships` 403 / auth error 10000, *not* clock skew). **Workaround:** use a `CLOUDFLARE_API_TOKEN` (the "Edit Cloudflare Workers" template) — token auth needs no callback. `CLOUDFLARE_API_TOKEN=… pnpm run deploy`.
- **Cross-repo deploy ordering is load-bearing.** The FE `netlify.toml` `/relay/*` rewrites could only be removed *after* the router was deployed with its `/relay` routes — remove them first and analytics 404s in the gap (the Worker still passes `/relay` to Netlify, which no longer proxies it). Encode the ordering in the PR body and a code comment, not just chat.

## Generalizable bullets (promote into the checklist above)

- [ ] **An edge layer on `/*` makes request *count* the binding constraint.** Know the platform's hard request caps; immutable caching doesn't reduce invocations on a Worker route. Keep the API on a subdomain that bypasses the front-end edge so a front-end edge outage can't take data down with it.
- [ ] **Keep high-volume, non-cacheable traffic off the hot edge path.** Analytics beacons (PostHog `/relay/*`) routed through a `/*` Worker are a large, invisible share of invocations.
- [ ] **`merge ≠ deploy` unless CI deploys.** Confirm each repo actually ships on merge; wire auto-deploy (Workers Builds) or you'll have a merged-but-undeployed fix mid-incident.
- [ ] **OAuth-callback CLI logins fail in headless / WSL / remote shells** (no reachable `localhost` callback). Use API-token auth for any cloud CLI here.
- [ ] **Cloudflare 1027 ≠ 1015.** 1015 = a rate-limit rule you set; 1027 = the account/plan request cap. Read the numeric code before choosing a fix.

---

<!-- Appended 2026-06-01 by the connection-exhaustion / 429-storm / migration
     session. The API + infra incidents from the same launch day (the stub
     checklist items above): the Cloud SQL connection cascade, the 429 storm,
     the self-blocking deploy deadlock, and the live-event migration discipline.
     Generalizable bullets at the end — promote into the checklist above. -->

## API / infra incident write-ups

> **Severity (API side):** two same-day, viewer-facing outages of the standings
> *data* — a morning connection-exhaustion outage (all endpoints 5xx) and an
> afternoon 429 storm (~80% of viewer reads rejected). Both resolved same-day;
> no data loss.

### Cloud Run stale-revision accumulation saturated Cloud SQL connections

- **Symptom:** every endpoint began 5xx'ing during the event; Cloud SQL pinned at its `max_connections=100` ceiling.
- **Impact:** full API outage (standings data down) until revisions were pruned.
- **Root cause:** each deploy creates a new Cloud Run revision, and each revision's spec bakes in `minScale=1 + cpu_idle=false` — so *every past* revision keeps a hot instance **and its DB connection pool** alive even at 0% traffic. Cloud Run never auto-prunes. 100+ stale revisions × ~5 connections each saturated the cap; every endpoint then failed to acquire a connection.
- **Why it wasn't caught earlier:** revisions accumulate invisibly across many deploys; the leak only bites when the count × pool crosses the DB cap, which first happened under event-day deploy frequency.
- **Fix:** #171 — a CI step pruning to the 2 newest revisions per service after each deploy (current + one rollback). #172 — tightened the per-instance pool to `2+1 = 3` for `maxScale=20` headroom.
- **Detect faster:** alert on Cloud SQL `num_backends` nearing the cap, and on Cloud Run revision count per service.
- **Lesson for future projects:** on Cloud Run a `minScale≥1` revision is **not free after it stops serving** — it pins an instance + its connection pool until deleted. Prune on every deploy; an un-pruned revision is a silent, compounding resource leak.

### 429 storm — limiter keyed on Cloudflare's edge IP + `Vary: Cookie` defeated CDN coalescing

- **Symptom:** ~80% of viewer reads (`/standings`, `/tournaments/{slug}`) returned `429 Rate exceeded`; the FE showed perpetual loading. 429 logs were all keyed on Cloudflare edge IPs (`104.23.x`, `162.158.x`).
- **Impact:** effective viewer outage during the event — the API answered, but rejected most reads.
- **Root cause (two compounding):** (1) the slowapi limiter used `get_remote_address` = `request.client.host`, which **behind Cloudflare is the CF edge IP** — so the whole audience shared a handful of `300/min` buckets and a live crowd tripped the cap for everyone. (2) the live reads emitted `Vary: Cookie` (to split admin vs viewer caching), which **fragments the CDN cache per unique cookie** — every viewer is a unique key → guaranteed miss → the `s-maxage=15` coalescing never engaged → every viewer hit origin, feeding (1).
- **Why it wasn't caught earlier:** both are latent until live-event concurrency; the limiter "works" in dev (one client = one IP), and `Vary: Cookie` looks like correct HTTP. A CDN also doesn't cache JSON at all without an explicit cache rule, so the intended coalescing was never actually in place.
- **Fix:** #176 — key the limiter on `CF-Connecting-IP` (→ left-most `X-Forwarded-For` → peer); drop `Vary: Cookie` so viewers share one cached copy. Follow-up #178 (open, low-urgency) — add the Cloudflare Cache Rule (cookie-agnostic cache key + bypass on the `criticalbit_access` admin cookie) for robust coalescing + admin read-after-write freshness.
- **Detect faster:** alert on 429 rate by endpoint; synthetic check that `cf-cache-status` trends to `HIT` under load.
- **Lesson for future projects:** a CDN-fronted API must rate-limit on the **real client IP** (the forwarded header), never the peer/edge IP — or one shared edge IP rate-limits your entire audience as one. `Vary: Cookie` on a cacheable response **silently defeats CDN coalescing**; do the admin/viewer split with a CDN cache rule (bypass-on-cookie), not `Vary`. Origin `Cache-Control` alone does not make a CDN cache JSON.

### The deploy deadlock — connection exhaustion blocked the very deploy that would fix it

- **Symptom:** the #176 fix wouldn't deploy — the CI `migrate` step failed ~4× with `asyncpg.exceptions.TooManyConnectionsError` ("remaining connection slots are reserved …").
- **Root cause:** the 429 storm kept the API scaled up, its pools consuming all 100 connection slots, so the migrate job couldn't get one → deploy failed → the fix couldn't land. A self-reinforcing deadlock: **the outage blocked its own remedy.**
- **Fix:** broke it by **raising the ceiling** — applied #173 (`tofu apply`: Cloud SQL `db-g1-small → db-custom-1-3840`, `max_connections 100 → 200`; a one-time ~5-min in-place restart), which gave the migrate job headroom; then re-ran the failed deploy and #176 landed, 429s → 0. (Manually pruning stale revisions to free slots is the other lever, used earlier the same day.)
- **Lesson for future projects:** a resource-exhaustion outage can silently block the deploy that fixes it. Keep a deliberate **headroom lever** (raise the cap, or free the resource by hand) so a fix can always be pushed through — and recognize the deadlock early instead of re-running a doomed deploy.

### Live-event schema migration — expand → transition → contract, never one destructive step

- **Symptom (avoided):** #167 re-keyed `team_members` from `profile_id` to a surrogate `tournament_player_id`. A single migration that *dropped* `profile_id` would have 5xx'd the core `/standings` endpoint for the ~30–90s deploy rollover, because the still-serving revision reads `profile_id` (via `_team_by_profile`).
- **Root cause / trap:** the "expand" step (#169) added the new column but **did not move the reads** off `profile_id` — so the deployed code still read it, and the original one-shot contract (draft #170) would have raced the rollover on the most-watched endpoint, mid-event.
- **Fix:** split into two zero-downtime deploys — #179 *transition* (swap the PK to `tournament_player_id`, make `profile_id` NULLABLE but **keep the column**, switch the reads), then #181 *contract* (drop the now-unused column once no serving revision reads it). Verified **0 × 5xx** through both rollovers. #184 then exposed `tournament_player_id` on the read endpoints so the FE could use the new key; #185 windowed `/progression` to the event dates.
- **Lesson for future projects:** expand-then-contract is really **three phases** — *expand* (add + dual-write), *transition* (move reads, keep the old column), *contract* (drop). Never drop a column the currently-serving revision still reads, especially mid-event. And re-keying an entity means the new key must be **readable wherever the consumer needs it** — we shipped the mutation contract (#179) before the read exposure (#184), which briefly broke the FE's team-management.
- **#187 — same playbook, this time a breaking contract + cross-repo FE:** the player-entity unification (drop the placeholder/polled `profile_id` XOR `name`; make every row one first-class entity keyed on `tournament_player_id`) reran the exact chain — #190 *expand* (drop the XOR check, backfill `name`), #191 *transition* (unified shape + `tournament_player_id` addressing), #192 *contract* (`name` NOT NULL + cleanup) — at **0 × 5xx** across all three rollovers, mid-event. Two things were harder than the #167 rekey: it broke the **API contract** (addressing moved to the surrogate id with no back-compat alias possible — `profile_id` and `tournament_player_id` are both ints and can't be told apart on `/players/{int}`), so we prepped the FE PR against the Phase-2 spec, held it CI-green, and deployed it **immediately after #191's rollover** to minimize the viewer-facing detail-page break window; and it surfaced two migration gotchas (own bullets below).

### CI pipeline amplifier — a failing migrate step silently skipped deploy + prune

- **Symptom:** because the `migrate` step failed (the deadlock), the dependent `Deploy to Cloud Run` and `Prune stale revisions` steps were **skipped** — so the pruning that would have relieved the connection pressure never ran, deepening the outage.
- **Lesson for future projects:** order pipeline steps so a failure in one doesn't disable the mitigation for that same failure. A recovery step (prune = resource relief) shouldn't be gated behind the consumer (migrate) whose pressure the relief would ease. Make recovery steps independent of the steps they recover.

## Generalizable bullets — API / infra (promote into the checklist above)

- [ ] **On Cloud Run, prune stale revisions every deploy.** A `minScale≥1` revision pins an instance + DB pool even at 0% traffic; accumulation silently saturates a connection cap. Keep N newest, delete the rest.
- [ ] **Rate-limit on the real client IP, not the peer/edge IP.** Behind a CDN/proxy, `request.client.host` is the edge IP — keying on it rate-limits the whole audience as one. Read `CF-Connecting-IP` / `X-Forwarded-For`.
- [ ] **`Vary: Cookie` defeats CDN coalescing.** It fragments the cache per cookie value; viewers never share an entry. Do the admin/viewer split with a CDN cache rule (bypass-on-cookie), not `Vary`. A CDN won't cache JSON without an explicit cache rule.
- [ ] **Keep a headroom lever for the self-blocking outage.** Resource exhaustion can block the deploy that fixes it; be able to raise the cap or free the resource by hand so a fix can always land.
- [ ] **Expand → transition → contract (three phases).** Never drop a column a serving revision still reads; move the reads in a middle deploy first. Verify each rollover is zero-5xx. Mid-event, always prefer this to a single destructive migration.
- [ ] **Ship a re-key's read exposure with (or before) its write contract.** A new mutation key the consumer can't *read* anywhere breaks the consumer (#179 landed before #184).
- [ ] **Migrations only run against prod — validate them on a throwaway Postgres.** The suite builds its schema via `metadata.create_all` (SQLite) and never runs Alembic, so a bad backfill/DDL passes CI and only fails at the deploy's `alembic upgrade head`. Spin up a throwaway `postgres:16` container, seed realistic pre-migration rows (incl. edge cases), upgrade, assert data + constraints, and test the downgrade. (#187)
- [ ] **Stacked migration PRs: don't rebase a code-only phase onto main if a later phase adds a migration.** Rebasing for a clean diff drops the earlier phase's migration file from the later branch's history, so its `down_revision` resolves to the wrong head. Base the migration-bearing phase on the earlier *migration* branch and cherry-pick the code-only phase onto it; the chain is valid on main once the phases merge in order. (#187)
- [ ] **Don't gate a recovery step behind the step it recovers.** A failing migrate that skips the prune removed the relief for the very pressure that failed the migrate.
- [ ] **Endpoints that aggregate a tournament must respect its date window.** `/progression` returned a player's whole tracked history (back to 2021) until windowed to `[start_date, grand_finals_date]` (#185) — mirror the same bound everywhere `tournament_record` already uses it.

## Action items / follow-ups (API / infra)

| Item | Owner | Status | Link |
|------|-------|--------|------|
| Add the Cloudflare Cache Rule (cookie-agnostic cache key + bypass on `criticalbit_access`) for robust coalescing + admin read-after-write freshness | infra | TODO (low-urgency — #176's `Vary` drop already restored coalescing) | [#178](https://github.com/ag-tech-group/aoe2-live-standings-api/issues/178) |
| Consider PgBouncer (or Cloud SQL connection pooling) if `maxScale` grows past the current 200-connection headroom | infra | future | — |
| Alerting: Cloud SQL `num_backends` vs cap; 429 rate; Cloud Run revision count per service | infra | TODO | — |
| Adopt a deploy-freeze around marquee matches (no API deploys at peak) | infra | DONE | [docs/deploy-freeze.md](deploy-freeze.md) ([#198](https://github.com/ag-tech-group/aoe2-live-standings-api/issues/198)) |
