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

## What's *not* in Terraform

- **Image deploys** — `gcloud run deploy --image ...` (see above).
- **Cloudflare DNS records** — manual via the Cloudflare dashboard. We don't manage the criticalbit.gg zone from Terraform.
- **Initial GCS state bucket** — created imperatively once, before any `tofu init` could run. Bootstrap chicken-and-egg.
- **Project + billing link** — same, one-shot bootstrap.

## Ongoing cost

~$8-10/month at idle, dominated by the Cloud SQL `db-f1-micro` instance. Cloud Run with `min_instance_count=1` runs continuously but the always-on CPU charge on a single small instance is in the dollar-per-month range. Artifact Registry storage and Secret Manager versions are free at this scale.
