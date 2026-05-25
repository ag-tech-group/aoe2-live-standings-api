# Event traffic & cost model

Reference doc for sizing GCP and Netlify costs against tournament-scale traffic, using the planned 2026 Hera-hosted cross-stream invitational as a worked example. Methodology and architecture analysis are reusable for any future event; specific roster numbers will drift.

**Status:** initial pass complete; final roster + bookings pending.
**Created:** 2026-05-24.
**Tracking issue:** [#84 — scaling and reliability hardening](https://github.com/ag-tech-group/aoe2-live-standings-api/issues/84)
**Related docs:** [data-sources.md](./data-sources.md)

## TL;DR

- **Baseline GCP cost is ~$105/mo** today, dominated by two always-on Cloud Run instances (API + worker) — not request volume. Event spike adds maybe $5–$140 depending on capture rate.
- **The real risk is not cost, it's DB read saturation.** At Scenario C (~5k–9k concurrent on-site), unmitigated polling traffic produces ~1,200–2,300 reads/sec against a `db-f1-micro` shared-core Postgres. It will fall over.
- **The cheapest, highest-leverage fix is enabling Cloudflare's orange-cloud proxy** on the API hostname. Free, eliminates the DB risk, preserves the existing `Cache-Control: public, max-age=15` semantics, and passes SSE through.
- **Netlify Pro is well-sized for the SPA** (~$20–$70 for the event month even at 730k visitors). It should **not** proxy the API or SSE — Netlify's `[[redirects]]` proxy has a hard 26-second timeout that kills SSE, and proxied bytes are billed twice (Cloud Run egress + Netlify bandwidth).
- **Single biggest product decision** for traffic: whether Hera puts the bracket URL on the stream overlay. That alone swings capture rate ~3×.

## What this doc is for

When an event drives a usage spike, three questions need fast answers: *will it stay up, how much will it cost, and what do we change beforehand?* This is the worked answer for the 2026 invitational and the template for the next one.

## Architecture today

| Layer | What | Where |
|---|---|---|
| API compute | Cloud Run `aoe2-live-standings-api`, us-central1, 1 vCPU / 512Mi, min=1 max=10, 800 concurrent/instance, CPU always-on, 1hr timeout | `infra/terraform/run.tf:25–174` |
| Worker compute | Cloud Run `aoe2-live-standings-api-worker`, same shape, min=max=1 (singleton), CPU always-on | `infra/terraform/run.tf:177–298` |
| Database | Cloud SQL Postgres 16, **db-f1-micro** (shared-core), 10 GB HDD, ZONAL (no HA), PITR on, Cloud SQL Auth Proxy via Unix socket | `infra/terraform/sql.tf:7–63` |
| Polling cadence (worker → upstream Relic API) | Player stats 30s · Recent matches 60s · Live matches 15s | `app/poller/*.py` |
| Real-time delivery | **SSE** at `GET /v1/stream`, 20s heartbeat, ~70 byte nudge payloads (`STANDINGS`, `MATCHES`, `LIVE`) | `app/routers/stream.py` |
| Caching | `public, max-age=15` on standings, `max-age=60` on completed matches, `no-store` on in-progress and SSE. No Redis, no in-process cache, **no CDN, Cloudflare DNS-only** | `app/routers/tournaments.py:52`, `app/main.py:126–141`, `infra/README.md:96` |
| Standings query path | 5 queries: roster + denormalized player+rating join + recent results + live matches + tournament record (N+1 avoided) | `app/routers/tournaments.py:340–404` |
| Rate limiting | slowapi, 60 RPM/IP default | `app/limiting.py:17` |
| Payload | StandingRow ~280 B/row × 32-player default ≈ **~9 KB uncompressed / ~2–3 KB gzipped** | `app/schemas/leaderboard.py:45–82` |
| Frontend (separate repo) | React 19 + Vite, hosted on **Netlify Pro**, served at `aoe2.criticalbit.gg`. Talks to API direct via CORS. | `/home/amr/code/aoe2/hera-streamer-invitational-2026-web` |

The architecture is intentionally SSE-driven: tiny nudges keep clients aware of changes, and clients refetch the heavy standings JSON on demand. The `Cache-Control: public, max-age=15` on standings is the hook that would let any CDN (Cloudflare, Cloud CDN, Netlify edge) coalesce client traffic into ~4 origin requests/minute regardless of viewer count — but nothing is currently honoring it at the edge.

## Audience model

### Roster

Tournament is a **cross-stream invitational** (confirmed). Final roster pending from organizer. Numbers below are 30-day windows ending 2026-05-24. Re-pull the week of the event before locking the model.

| Streamer | Twitch handle | TW ACV | YT subs | YT live? | Effective event ACV | Status | Confidence |
|---|---|---:|---:|---|---:|---|---|
| **Hera** *(host)* | `hera` | 3,400 | 229–245k | No (VODs) | **15,000** *event-amplified* | HOST | MED |
| Tyler1 | `loltyler1` | 9,800 | 2.74M | No (TW exclusive) | 9,800 *if streams* | PENDING | LOW participation |
| SpiffingBrit | `thespiffingbrit` (~dormant TW) | n/a | **4.41M** | **Yes** | **8,000–12,000** YT-dominated | PENDING | LOW live CCV |
| Grubby | `grubby` | 7,100 (~3.5–5k AoE-specific) | 418k | Occasional simulcast | 4,000 AoE2-specific | CONFIRMED+practicing | MED |
| Bonjwa | `bonjwa` | 4,245 | 65k | No | ~2,500 (DE-language discount) | PENDING | MED |
| Atrioc | `atrioc` | 3,200 | 910k | No (VODs ≠ concurrent) | 3,200 TW-only | CONFIRMED | HIGH |
| SingSing | `singsing` | 2,180 | no first-party live | No | ~0 AoE2-effective | CONFIRMED+practicing | HIGH |
| Pestily | `pestily` | 1,100 | 763k | No | 1,100 *if streams* | PENDING | LOW participation |
| Ahmpy | `ahmpy` | 850 | small | No | ~850 | PENDING | MED |
| Day9 | `day9tv` | 710 | 576k | No | ~710 | CONFIRMED+practicing | LOW will participate |
| YamatoCannon | `yamatocannon` | 750 | 68k | No | ~750 | CONFIRMED | MED |
| Knoff | `knoff` *(WC3, not AoE2)* | 465 | n/a | n/a | ~500 *if same person* | CONFIRMED+practicing | LOW identity |
| Uthermal | `uthermalsc2` | 400 | 179k | No | ~400 | CONFIRMED | HIGH |
| PiG | `x5_pig` | 380 | ~100k | Rarely | ~400 | CONFIRMED | HIGH |
| ChessBrah | `chessbrah` | 369 | 342–405k | Sometimes (chess) | ~0 AoE2-effective | PENDING | HIGH irrelevance |
| Cooper | `cooper_aoe` (dormant) | n/a | n/a | n/a | **needs manual** | CONFIRMED+practicing | LOW |
| Gunnar | not located | n/a | n/a | n/a | **needs manual** | CONFIRMED+practicing | LOW |
| Deathnote | not located | n/a | n/a | n/a | **needs manual** | CONFIRMED+practicing | LOW |

Effective event ACV uses a 70% Twitch↔YouTube overlap discount: YT additive = 30% of YT live CCV. For pure-Twitch streamers, YT additive is 0.

### Reach totals (additive across broadcasts, not concurrent)

| Variant | Reach |
|---|---:|
| CONFIRMED-only, excluding Hera | ~9,960 (+ Cooper/Gunnar/Deathnote unknowns) |
| CONFIRMED + Hera | ~24,960 |
| CONFIRMED + PENDING + Hera | ~49,210 |

Reach is *additive* across broadcasts. Peak concurrent during a single marquee match is *less than* reach because (a) not everyone streams the same match, (b) audiences overlap, (c) co-streams attenuate as matches drag. **Use peak concurrent for capacity, use reach for daily-unique modeling.**

### Notes on the previous pass

Most of the original brief's ACVs were over-estimated 2–6×, probably from pandemic-era peaks:

| Streamer | Original brief | Verified 2026-05-24 |
|---|---:|---:|
| Pestily | 7,000 | 1,100 |
| Atrioc | 9,000 | 3,200 |
| ChessBrah | 3,000 | 370 |
| Uthermal | 2,500 | 400 |
| Day9 | 1,500 | 710 |
| Bonjwa | 2,000 | 4,245 *(but DE-language)* |

## Traffic model

### Variables (relabel and re-run)

| Symbol | Low | Expected | High | Source |
|---|---:|---:|---:|---|
| `PEAK_CCV` peak combined-platform broadcast CCV at marquee match | 15,000 | 30,000 | 60,000 | Hera-tier → cross-game roster → full pending lineup |
| `CAPTURE` % of broadcast CCV with bracket open | 3% | 5% | 8% passive / **15% active** | Altar of Champions benchmark + second-screen literature |
| `MULT` daily uniques / peak CCV | 3× | 5× | 8× | Twitch UAV + AoE2 second-screen sites |
| `EVENT_DAYS` event days per month | 4 | 6 | 10 | Group stage + finals |
| `BASELINE_DAU` quiet-day visitors | 50 | 200 | 500 | Hype + scrim casts |
| `SESSION_HRS` avg session length | 0.5 | 1.0 | 2.0 | Marquee matches ~3hr; viewers don't watch all |
| `REFRESH_RATE` standings refetches/viewer/hour | 60 | 120 | 240 | SSE nudges drive refetch; pollers fire 15–60s |

### Derived numbers

```
PEAK_ONSITE = PEAK_CCV × CAPTURE
EVENT_DAU = PEAK_CCV × CAPTURE × MULT
MONTHLY_VISITS = (EVENT_DAYS × EVENT_DAU) + (QUIET_DAYS × BASELINE_DAU)
EVENT_STANDINGS_REQS = EVENT_DAYS × EVENT_DAU × SESSION_HRS × REFRESH_RATE
```

| Scenario | Peak on-site | Event-day DAU | Monthly visits | Standings reqs/mo |
|---|---:|---:|---:|---:|
| Low | 450 | 1,350 | 6,700 | ~182k |
| Expected | 1,500 | 7,500 | 49,800 | ~5.5M |
| High (passive promo) | 4,800 | 38,400 | 394,000 | ~74M |
| **High (active promo)** | **9,000** ⚠ | 72,000 | 730,000 | **~138M** |

The High-active scenario peaks **above the 8,000-seat SSE capacity** (800/instance × 10 max instances). The 138M figure also represents ~700M DB queries if uncached.

## GCP cost model

### Pricing assumptions (us-central1, Tier 1, late 2026)

| Item | Rate |
|---|---|
| Cloud Run CPU always-on | $0.0000180 / vCPU-second |
| Cloud Run memory always-on | $0.00000200 / GiB-second |
| Cloud Run requests | $0.40 / million |
| Free tier (monthly) | 240k vCPU-sec, 450k GiB-sec, 2M requests |
| Cloud SQL db-f1-micro | ~$0.0105 / hr |
| Cloud SQL HDD storage | $0.09 / GB-mo |
| Internet egress NA, 0–1TB | $0.12 / GB |
| Cloud CDN cache egress NA | $0.08 / GiB |
| HTTPS LB (forwarding rule) | $0.025 / hr (~$18.25/mo) |
| **Cloudflare proxy (orange-cloud)** | **$0 free tier** |

### Itemized baseline (idle)

| Line | Math | Cost/mo |
|---|---|---:|
| API: 1 always-on, 1 vCPU | (2.628M − 240k free) × $0.0000180 | $42.98 |
| API: 0.5 GiB memory | (1.314M − 450k free) × $0.00000200 | $1.73 |
| Worker: 1 always-on, 1 vCPU (free tier consumed) | 2.628M × $0.0000180 | $47.30 |
| Worker: 0.5 GiB memory | 1.314M × $0.00000200 | $2.63 |
| Cloud SQL db-f1-micro instance | 730 × $0.0105 | $7.67 |
| Cloud SQL HDD 10 GB | 10 × $0.09 | $0.90 |
| Cloud SQL backups / PITR | ~25% data | $0.50 |
| Egress | ~1 GB × $0.12 | $0.12 |
| Logs, Artifact Registry, Secret Manager | free tier | $0.00 |
| **Baseline total** | | **~$104/mo** |

### Event-month spike scenarios

| Scenario | Compute burst | Requests | Egress | DB load | Spike $ | Outcome |
|---|---|---|---|---|---:|---|
| Low | +5 instance-hr | 182k (free tier) | 0.5 GB | 6 reads/sec | +$1 | Fine |
| Expected | +20 instance-hr | 5.5M ($2.20) | 16 GB ($1.92) | 80 reads/sec | +$5.50 | Stressed but holds |
| High (passive) | +300 instance-hr | 74M ($29.60) | 222 GB ($26.64) | 1,200 reads/sec ⚠ | +$77 | **DB falls over** |
| High (active) | 12 instances needed; **max=10 caps it** | 138M ($55.20) | 414 GB ($49.68) | 2,300 reads/sec ⚠ | +$140 | **DB fails + 503s on SSE** |

The cost ceiling for the event month is **~$245/mo even at the worst case** — well within budget. The failure mode is degraded service, not surprise spend.

### The CDN delta

Scenario C-passive with vs without a CDN layer:

| Line | No CDN (today) | With CDN |
|---|---:|---:|
| Origin requests to standings | 74M | ~40k |
| DB queries triggered | ~370M | ~200k |
| DB read load peak | 1,200/sec ⚠ | ~0.07/sec |
| Cloud Run egress | 222 GB × $0.12 = $26.64 | ~120 MB ≈ $0.01 |
| CDN egress (if Cloud CDN) | n/a | 222 GiB × $0.08 = $17.76 |
| CDN egress (if Cloudflare) | n/a | $0 (Cloudflare absorbs) |
| HTTPS LB base (if Cloud CDN) | $0 | $18.25 |
| **Total egress cost** | $26.64 | ~$36 (Cloud CDN) / ~$0 (Cloudflare) |
| **DB reliability risk** | HIGH | NEGLIGIBLE |

A real CDN doesn't save much money at this scale — it eliminates a *failure mode*. **Cloudflare orange-cloud is the obvious choice** because it costs nothing and requires no GCP infrastructure changes.

## Frontend / Netlify Pro

The SPA at `aoe2.criticalbit.gg` is hosted on Netlify Pro. Investigated 2026-05-24.

### Today's frontend config

| Aspect | Value | File |
|---|---|---|
| Stack | React 19 + Vite 8, TanStack Router + Query, Sentry, PostHog | `package.json:11` |
| Netlify config | No `netlify.toml`; only `public/_redirects` + `public/_headers` | `public/_redirects:1`, `public/_headers:1–6` |
| Routing | `/* /index.html 200` (SPA catch-all) | `public/_redirects:1` |
| Security headers | CSP, X-Frame-Options DENY, nosniff, Referrer-Policy, Permissions-Policy | `public/_headers:1–6` |
| API base URL | `VITE_API_URL=https://aoe2-live-standings-api.criticalbit.gg` | `.env.local` |
| API client | Ky + Orval-generated React Query hooks, 30s request timeout, `credentials: "include"` | `src/api/orval-client.ts` |
| SSE | Browser `EventSource("…/v1/stream")` **direct to API** | `src/hooks/use-live-updates.ts:67` |
| React Query staleTime | 5 minutes; invalidated on SSE nudge | `src/main.tsx:40` |
| Cache-Control headers | **None set** | gap in `public/_headers` |
| Bundle | main 130 KB gzip, api 35, posthog 75, css 25, all hash-named per Vite | `scripts/size-check.mjs` |

The browser talks to the API direct via CORS; SSE bypasses Netlify entirely. Netlify is serving only the static SPA bundle.

### Netlify Pro pricing model (April 2026)

Pro is **$20/mo flat with 3,000 credits included**:

| Item | Credit cost |
|---|---|
| Web bandwidth | 20 credits / GB |
| Web requests | 2 credits / 10,000 |
| Production deploy | 15 credits |
| Form submissions | Free |
| Deploy previews | Free |

3,000 credits ≈ 150 GB bandwidth if spent entirely on bandwidth. **Excess credits do not roll over.** Overages aren't billed per-GB — they're handled via **auto-recharge: 1,500 credits / $10** (~$0.13/GB equivalent). Auto-recharge is **off by default**; if it stays off and credits hit zero, **every web project on the team is paused**.

### Likely Netlify bill for the event month

Assuming the SPA-only architecture (SSE direct to GCP):

| Scenario | Uniques / mo | Avg KB / unique | Bandwidth | Requests | Credits | Cost |
|---|---:|---:|---:|---:|---:|---:|
| Low | 50,000 | 400 KB | ~20 GB | ~500k | ~530 | **$20** (Pro flat) |
| Expected | 250,000 | 500 KB | ~125 GB | ~3M | ~3,130 | **~$30** ($20 + 1× auto-recharge) |
| High (active) | 730,000 | 500 KB | ~365 GB | ~9M | ~9,130 | **~$70** ($20 + ~5× auto-recharge) |

Crossover at ~140 GB / ~250–300k uniques exhausts included credits. Even the worst case lands at **~$70 for the event month** — Netlify is cheap for this workload.

### Critical architecture finding: don't proxy API/SSE through Netlify

A natural-looking optimization would be configuring Netlify as a CDN in front of the API via `[[redirects]]`:

```toml
[[redirects]]
  from = "/api/*"
  to = "https://aoe2-live-standings-api.criticalbit.gg/:splat"
  status = 200
  force = true
```

**Do not do this.** Three blockers:

1. **Hard 26-second timeout on `[[redirects]]` proxy.** SSE connections (which target 1 hour) get killed every 26s. Confirmed by Netlify staff in multiple forum threads.
2. **Proxied bytes are billed twice.** Netlify staff confirmed: bytes flowing through a Netlify rewrite count against the bandwidth quota in both directions. You'd pay Cloud Run egress (~$0.12/GB) *and* Netlify bandwidth (20 credits/GB ≈ $0.13/GB on auto-recharge) for the same traffic.
3. **Netlify edge cache is per-node, not tiered.** With 70+ PoPs and a 15s standings TTL, hit ratio across all nodes is worse than Cloudflare/Cloud CDN. Per Netlify staff: *"cache is localized to individual CDN nodes, not shared across the network."*

**Correct architecture is the current one:** Netlify serves the SPA, browser talks API + SSE direct to GCP. If we want an edge cache in front of the API, **Cloudflare orange-cloud is the right answer**, not Netlify proxy.

### Frontend gaps worth fixing

1. **No `Cache-Control` headers** on the SPA in `public/_headers`. Netlify defaults to `max-age=0, must-revalidate`, so every page load revalidates Vite's hashed (already-immutable) bundles. Easy fix:
   ```
   /assets/*
     Cache-Control: public, max-age=31536000, immutable
   /index.html
     Cache-Control: public, max-age=0, must-revalidate
   ```
2. **No `netlify.toml`.** Config is in `_redirects` + `_headers` only. Working today, but `netlify.toml` is the more featureful place to add rate-limiting rules, headers per build context, or a `[build.environment]` block.
3. **TanStack Query `staleTime: 5min` + SSE-driven invalidation** means refetch storms are possible if SSE nudges queue or burst (1000 viewers all invalidating at once). Probably fine; worth monitoring during the first event.

### Netlify red flags during the event

- **No spending cap.** Auto-recharge on = unbounded auto-billing on viral days. Auto-recharge off = projects pause when credits hit zero, mid-event. **Pick one and monitor.**
- **Cache purges on every deploy** unless `Netlify-Cache-ID` is set. Don't deploy during the event.
- **5 rate-limit rules max on Pro.** Plan rule layout before event week.
- **Netlify Analytics availability under credit-based pricing is ambiguous.** PostHog is already wired in the SPA (`src/lib/posthog.ts`); rely on that for event analytics.

## Benchmarks against comparable events

| Event | Date | Peak broadcast CCV | Companion site | Implied capture |
|---|---|---:|---|---|
| **Altar of Champions (WC3)** *strongest analog* | 2026-05-16/17 | ~50–80k combined (Grubby ~28.7k peak alone) | `grubby-poc.azurewebsites.net/AltarOfChampions` (~8.3k April monthly visits via Similarweb) | **~2–5% passive** |
| Hera's *Champions Invitational* | 2023-08 | 13,645 peak | Liquipedia + aoe2cm.net | n/a |
| Red Bull Wololo: Londinium | 2026-04 | 115,944 peak / 40,668 avg | Liquipedia only | n/a |
| T90 Titans League 5 | 2026-02/03 | 24,167 peak / 10,425 avg | Liquipedia only | n/a |
| Nili's Apartment Cup V | 2024-01 | 51,381 peak | Liquipedia only | n/a |
| aoe2cm.net (sitewide AoE2 second-screen baseline) | 2026-04 | n/a | n/a | **+70% MoM** aligned with Wololo: Londinium |
| Squid Craft 2 (Twitch UAV anchor) | 2023 | 1.6M peak / 1.1M avg | n/a | UAV/peak ≈ 3.1×, UAV/avg ≈ 4.5× |

### Implied parameters

- **Capture rate, passive promotion: 3–6%** (MEDIUM-LOW confidence, n=1 hard anchor). Original 3–8% baseline is correct.
- **Capture rate, active promotion: 8–15%** (LOW confidence, extrapolated). Single product decision swings ~3×.
- **Daily uniques / peak CCV: 4–6×** (LOW confidence). The generic 5–15× rule-of-thumb is probably high for a 3–4 day single-host event.
- **Hera-hosted invitational peak combined CCV: 15–40k expected, 40–80k upper bound** with cross-game co-streamers. Below Wololo's 116k tier; above Hera's solo 8.7k.

The single highest-value data point still missing: actual hourly traffic to `grubby-poc.azurewebsites.net` during May 15–17. If we can get it from Grubby's team, it replaces the capture-rate guesstimate with a hard number for a near-identical event.

## Recommendations

### Pre-event

1. **Enable Cloudflare proxy** on both `aoe2.criticalbit.gg` and `aoe2-live-standings-api.criticalbit.gg`. Free, removes the DB risk, preserves Cache-Control semantics, passes SSE through. Smoke-test SSE under proxy before flipping in prod.
2. **Confirm roster handles**, especially Cooper / Deathnote / Gunnar / Knoff. These swing the reach model by ~5k.
3. **Decide on URL promotion intensity.** If active (overlay + chat command + on-air plugs), aim for 8–15% capture and provision for the upper scenarios. Frame as a product/marketing call, not a model input.
4. **Add `Cache-Control` headers to the SPA** via `public/_headers` (immutable for `/assets/*`, must-revalidate for `/index.html`).
5. **Toggle Netlify auto-recharge on, monitor daily during event days.**
6. **Optional (insurance):** Consider raising `max_instance_request_concurrency` from 800 to ~1,200 *or* `max_instances` from 10 to 15 if the model lands in High-active. Stress-test memory first.
7. **Optional (insurance):** Bump Cloud SQL from db-f1-micro to db-g1-small for the event month (+~$25/mo). Reverts after.

### During the event

8. Watch Cloud SQL CPU and active connections in GCP console. db-f1-micro should sit well below 50% even at Expected; if it spikes during marquee matches, the cache isn't working.
9. Watch Cloud Run concurrent connections per instance. If saturating at 800, raise concurrency.
10. Watch Netlify credit consumption daily.
11. Don't deploy.

### Post-event

12. Pull Cloudflare analytics for actual capture rate. First hard anchor for *your* audience; record for next event.
13. Pull Netlify bandwidth report to validate the SPA-only cost model.

## Open questions / needs manual input

| # | Item | Why it matters |
|---|---|---|
| 1 | Final roster + canonical Twitch URLs for Cooper / Deathnote / Gunnar / Knoff | Swings reach ~5k |
| 2 | Confirm Tyler1 / SpiffingBrit participation | 2–3× upper-bound reach |
| 3 | SpiffingBrit YT live CCV (last 3 livestreams) | Largest YT-side number; could be 5k or 20k |
| 4 | Grubby's May 15–17 hourly traffic to grubby-poc.azurewebsites.net | Replaces capture-rate guess with real number |
| 5 | Hera's current `hera` Twitch follower count at event-time | Baseline for event-amplified estimate |

## Sources

- twitchtracker.com / sullygnome.com / socialblade.com — viewer metrics (2026-05-24)
- escharts.com — tournament viewership data
- liquipedia.net — tournament rosters and history
- similarweb.com/website/grubby-poc.azurewebsites.net — companion-site traffic anchor
- GCP pricing pages (Cloud Run, Cloud SQL, networking) — us-central1 Tier 1
- docs.netlify.com — credit-based pricing, rewrites/proxies, caching, edge functions
- answers.netlify.com forum threads on SSE timeouts, proxy bandwidth billing, per-node cache
- /home/amr/code/aoe2/hera-streamer-invitational-2026-web — frontend repo

## History

- 2026-05-24 — initial pass. Roster pending. Netlify Pro architecture analyzed and confirmed correct (SPA on Netlify, API+SSE direct to GCP).
