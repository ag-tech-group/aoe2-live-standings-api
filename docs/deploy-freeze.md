# Deploy-Freeze Policy — Live-Event Peaks

## Policy

**No API deploys to `main` during scheduled marquee-match peak windows.** A
deploy is a Cloud Run revision rollover; during peak viewership that is the
single most dangerous routine action we take. Land changes in an off-peak
lull instead.

## Why (what 2026-06-01 proved)

- Every deploy creates a new Cloud Run revision, and **SSE stickiness keeps
  the old revision alive** — viewers hold their open `/v1/stream` connection,
  so the old and new revisions coexist, each with its **own DB connection
  pool**. During the 20:00 deploy flurry `num_backends` spiked to **153–166**
  (toward the then-200 cap).
- The CI `migrate` step (`alembic upgrade head`) contends for the same
  connection slots. On launch day this produced the **deploy deadlock**:
  connection exhaustion from the 429 storm starved the migrate job
  (`TooManyConnectionsError`, ~4× failures), so the very fix that would end
  the storm couldn't deploy. The outage blocked its own remedy.

Net: deploying at peak both spikes connections on the most-watched surface
*and* risks a state where you can't deploy at all.

## The freeze window

- **Freeze** spans each scheduled marquee match: warmup → match → cooldown.
  Derive the times from the published match schedule. Default span: from
  **30 min before** a marquee match until **30 min after** its expected end.
- **Lulls** (e.g. overnight between match days) are the deploy windows.
  **Batch** infra/app changes into a lull and verify before it closes.

## What to do instead

- Land and verify changes during a lull, well before the next peak.
- Apply bundled capacity changes (`maxScale` / concurrency / PgBouncer) via
  `tofu apply` in a lull, and verify `num_backends` + live health
  (`/v1/tournaments/current/standings`, the event page) before the window
  reopens.

## Emergency override (a hotfix that must ship during a freeze)

A freeze is a default, not a hard lock. If a fix genuinely can't wait:

1. **Decide consciously** — the cost is a rollover + connection spike on the
   most-watched endpoints, mid-match.
2. **Check headroom first** — confirm `num_backends` is well under the Cloud
   SQL cap. If it isn't, use the headroom lever (prune stale revisions or
   raise the tier) *before* deploying.
3. **Watch the migrate step** — if it fails on `TooManyConnectionsError`
   you're in the deadlock: free slots (prune / raise cap) before retrying.
   Never re-run a doomed deploy — that just deepens the pressure.

## Cross-service

This is one of three companion policies. The front end
(`hera-streamer-invitational-2026-web`) and the edge router
(`criticalbit-router`) carry their own peak-deploy risks (CDN cache purge +
stale code-split chunks; a Worker redeploy risking a whole-site edge
outage). **Freeze all three together** around a marquee match.

## Optional enforcement (future)

The policy is currently **manual discipline**. An opt-in CI guard could
enforce it — a `deploy`-job pre-check that fails (with a clear override path)
when a `DEPLOY_FREEZE` repo variable is set, or when `now()` falls inside a
configured event-window schedule. Deliberately **not added yet**: a blocking
guard on the deploy path is itself a deploy-path change, so it wants its own
calm-window rollout *and* an always-available override — the deadlock lesson
is that tooling must never be able to block a genuine emergency fix.

## References

- `docs/launch-lessons-learned.md` — the deploy deadlock; the CI pipeline
  amplifier; the stale-revision → connection-exhaustion incident.
- #198 (this policy); companion deploy-freeze issues in the FE and router
  repos.
