# Infrastructure

Terraform + manual deploy steps for the AoE2 Live Standings API preview on GCP.

## Layout

```
infra/
├── terraform/         # All persistent GCP resources except the deployed image
│   ├── main.tf        # Provider + GCS-backed state
│   ├── variables.tf
│   ├── sql.tf         # Cloud SQL Postgres 16 (db-f1-micro)
│   ├── registry.tf    # Artifact Registry Docker repo
│   ├── iam.tf         # Cloud Run service account + bindings
│   ├── secrets.tf     # DATABASE_URL in Secret Manager
│   ├── run.tf         # Cloud Run service (image managed out-of-band)
│   └── outputs.tf
└── README.md          # This file
```

## Prerequisites

- `gcloud` CLI authenticated as `amr@agtechgroup.solutions`:
  ```sh
  gcloud auth login amr@agtechgroup.solutions
  gcloud auth application-default login --account=amr@agtechgroup.solutions
  ```
- `tofu` (or `terraform`) on `$PATH`.
- `docker` running locally for the image build.

## Terraform

```sh
# One-time init (downloads providers, wires the GCS backend).
tofu -chdir=infra/terraform init

# See what would change.
tofu -chdir=infra/terraform plan

# Apply.
tofu -chdir=infra/terraform apply
```

State lives in `gs://aoe2-live-standings-api-tfstate/terraform/state` with versioning enabled.

## Build + push the image

```sh
PROJECT=aoe2-live-standings-api
REGION=us-central1
REPO=$REGION-docker.pkg.dev/$PROJECT/$PROJECT
TAG=$(git rev-parse --short HEAD)

# One-time per machine — teach docker how to auth to Artifact Registry.
gcloud auth configure-docker $REGION-docker.pkg.dev --account=amr@agtechgroup.solutions

# Build + push.
docker build -t $REPO/api:$TAG -t $REPO/api:latest .
docker push $REPO/api:$TAG
docker push $REPO/api:latest
```

## Deploy to Cloud Run

**Deploys are automated.** Every push to `main` runs `.github/workflows/ci.yml`, whose `deploy` job — gated on `lint` and `test` passing — builds the image, pushes it to Artifact Registry, and rolls a new Cloud Run revision. Auth is keyless via Workload Identity Federation (`infra/terraform/cicd.tf`); there is no service-account key in repo secrets.

The build + push steps above are only needed for a **manual deploy** (CD outage, or deploying an un-merged branch). Cloud Run's image attribute is intentionally outside Terraform (`lifecycle.ignore_changes` in `run.tf`), so a manual roll is just:

```sh
gcloud run deploy aoe2-live-standings-api \
  --image=$REPO/api:$TAG \
  --region=$REGION \
  --account=amr@agtechgroup.solutions \
  --project=$PROJECT
```

The container's `start.sh` runs `alembic upgrade head` before starting `uvicorn`, so migrations land automatically on each deploy (CD or manual). If a migration fails the container won't start and the new revision won't take traffic — Cloud Run keeps the old revision alive.

## Smoke test

```sh
SERVICE_URL=$(tofu -chdir=infra/terraform output -raw service_url)
curl -s $SERVICE_URL/v1/leaderboards | jq '.items | length'
curl -s $SERVICE_URL/v1/players | jq '.items | length'

# Watch live logs (polling cycles).
gcloud run services logs read aoe2-live-standings-api \
  --region=$REGION \
  --account=amr@agtechgroup.solutions \
  --project=$PROJECT \
  --limit=50
```

## Custom domain (`aoe2-live-standings-api.criticalbit.gg`)

Cloudflare manages `criticalbit.gg` DNS. After the Cloud Run service is up:

```sh
gcloud beta run domain-mappings create \
  --service=aoe2-live-standings-api \
  --domain=aoe2-live-standings-api.criticalbit.gg \
  --region=$REGION \
  --account=amr@agtechgroup.solutions \
  --project=$PROJECT
```

This prints DNS records (typically a CNAME) that need to be added in the Cloudflare dashboard. Cloud Run provisions a managed TLS cert automatically once DNS verification completes (usually <15 minutes).

### Cloudflare proxy mode (orange-cloud)

The DNS record for `aoe2-live-standings-api.criticalbit.gg` is set to **proxied** (orange-cloud, not grey-cloud) so Cloudflare's edge cache can absorb polling traffic. This is the cheapest single lever against the event-window failure mode modeled in [`docs/event-traffic-cost-model.md`](../docs/event-traffic-cost-model.md) and tracked under #84: the live endpoints set `Cache-Control: public, s-maxage=15, max-age=0, must-revalidate` (see `_STANDINGS_CACHE_CONTROL` in `app/routers/tournaments.py` and the matching constants in `players.py`, `leaderboards.py`, `live.py`, `matches.py`), so a proxy that honors `s-maxage` coalesces viewer traffic into ~4 origin requests/minute per endpoint regardless of concurrent viewer count.

**SSL mode** in the Cloudflare dashboard is set to **Full** (or stricter) — Cloudflare connects to the origin over TLS, validating against Cloud Run's Google-issued cert. **Do not set this to "Flexible"**: Cloud Run rejects plain-HTTP origin connections and the site would 526 / 502 the moment proxy mode is on. The edge cert is a Let's Encrypt wildcard (`*.criticalbit.gg`) configured on the zone — auto-renewing.

### Cloudflare Cache Rule (required for API caching)

**Without this rule, the orange-cloud proxy gives no caching benefit on our endpoints.** Cloudflare's default is to cache only a static-asset allowlist (CSS, JS, images, fonts); API responses get `cf-cache-status: DYNAMIC` (= ineligible) regardless of what `Cache-Control` the origin sends. To make CF respect our `s-maxage`, the zone has a Cache Rule:

- **Location in dashboard:** Caching → Cache Rules
- **Name:** `aoe2-live-standings-api: respect origin Cache-Control`
- **Match (both required):**
  - Hostname **equals** `aoe2-live-standings-api.criticalbit.gg`
  - URI Path **does not start with** `/v1/stream`  (defense-in-depth — the stream sends `Cache-Control: no-store`, but excluding by path means a future regression to that header can't have CF accidentally buffer the SSE stream)
- **Then:**
  - Cache eligibility: **Eligible for cache**
  - Edge TTL: **Use cache-control header from origin, bypass cache if not present**
  - Browser TTL: **Use cache-control header from origin, bypass cache if not present**

The "bypass if not present" choice is deliberate: the app's `cache_control_middleware` (`app/main.py`) stamps `public, max-age=3600` on every successful GET that doesn't set its own header, so the only response without `Cache-Control` is a non-200 — and we never want CF caching errors (5xx) past the origin's recovery.

**Verify via:**

```sh
URL=https://aoe2-live-standings-api.criticalbit.gg/v1/tournaments/default/standings
for i in 1 2 3; do
  curl -sI --max-time 8 "$URL" | grep -iE "cf-cache-status|x-request-id"
  sleep 2
done
# Expected: MISS on #1, HIT (same x-request-id) on #2 and #3.
```

### Cloudflare Cache Rule — admin bypass (cookie present)

The rule above lets CF cache live-endpoint responses for viewers, which is what protects origin at event-window scale. But **admins reading right after a mutation must skip the edge cache** — otherwise CF serves the cached viewer response (populated by recent viewer load) and the admin sees stale data until `s-maxage` elapses. The origin's `private, no-store` Cache-Control on authenticated requests can't fix this on its own: CF's cache lookup happens before origin is consulted, and CF's default cache key is URL-only (no cookies), so the cookie that distinguishes admin from viewer is invisible to the cache.

A second Cache Rule, **ordered above** the one above, bypasses cache when the access cookie is present:

- **Location in dashboard:** Caching → Cache Rules (this rule must be **first** in the list — rules run top-down and the first match wins)
- **Name:** `aoe2-live-standings-api: bypass cache for authenticated requests`
- **Match (all required):**
  - Hostname **equals** `aoe2-live-standings-api.criticalbit.gg`
  - Cookie `criticalbit_access` value matches regex `.+` (i.e., any non-empty value — "cookie is present")
- **Then:**
  - Cache eligibility: **Bypass cache**

Why "value matches regex `.+`" rather than just "cookie exists": CF's UI doesn't expose a bare "cookie exists" predicate, but the regex form is the documented equivalent.

The two-rule setup means:

- Viewer request (no `criticalbit_access` cookie) → first rule doesn't match → falls to second rule → cached per origin's `Cache-Control`.
- Admin request (cookie present) → first rule matches → bypass cache entirely → origin always sees the request → fresh data.

**Verify via:**

```sh
URL=https://aoe2-live-standings-api.criticalbit.gg/v1/tournaments/default/players
# Viewer-path: should populate then hit cache
curl -sI --max-time 8 "$URL" | grep -iE "cf-cache-status|cache-control"  # MISS or HIT
# Admin-path: should bypass on every request (substitute any non-empty value)
curl -sI --max-time 8 -H "Cookie: criticalbit_access=test" "$URL" | grep -iE "cf-cache-status|cache-control"
# Expected: cf-cache-status: BYPASS on every admin-path request.
```

### SSE compatibility through Cloudflare

Cloudflare's free tier supports `text/event-stream` and respects our 20s heartbeat (`_HEARTBEAT_INTERVAL_SECONDS` in `app/routers/stream.py`) through the proxy. The 100s default idle-timeout on free plans is well above the heartbeat cadence, so connections stay healthy indefinitely. The SSE endpoint sends `Cache-Control: no-store` *and* is excluded from the Cache Rule above — both prevent CF from buffering the stream.

### Rollback

Each piece of the CF setup can be undone independently in the dashboard (no DNS TTL wait — changes propagate in seconds):

- **Proxy mode** — toggle the DNS record from orange-cloud back to **DNS-only (grey-cloud)**. Origin connectivity is unaffected; only edge caching and CF routing are bypassed.
- **Cache Rule (respect-origin)** — disable or delete. CF falls back to the static-asset default (no API caching), but the proxy remains in path.
- **Cache Rule (admin-bypass)** — disable or delete. Admin requests would then hit the same cached responses as viewers, reintroducing #105's read-after-write staleness. The origin still sends `private, no-store` for authenticated callers, but CF answers from cache before consulting origin headers.
- **SSL mode** — leave at Full/Strict; don't drop to Flexible (see above).

## What's *not* in Terraform

- **Image deploys** — `gcloud run deploy --image ...` (see above).
- **Cloudflare DNS records, proxy mode, SSL mode, and Cache Rule** — all configured manually in the Cloudflare dashboard. The `criticalbit.gg` zone is not Terraform-managed. See the "Custom domain" section above for what's configured and how to re-create it.
- **Initial GCS state bucket** — created imperatively once, before any `tofu init` could run. Bootstrap chicken-and-egg.
- **Project + billing link** — same, one-shot bootstrap.

## Ongoing cost

~$8-10/month at idle, dominated by the Cloud SQL `db-f1-micro` instance. Cloud Run with `min_instance_count=1` runs continuously but the always-on CPU charge on a single small instance is in the dollar-per-month range. Artifact Registry storage and Secret Manager versions are free at this scale.
