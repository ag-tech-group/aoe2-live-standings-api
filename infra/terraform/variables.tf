variable "project_id" {
  description = "GCP project ID."
  type        = string
  default     = "aoe2-live-standings-api"
}

variable "region" {
  description = "GCP region for all resources. Single-region for preview-scale; revisit if the consumer team needs multi-region failover."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name. Used as the Artifact Registry repo name too for symmetry."
  type        = string
  default     = "aoe2-live-standings-api"
}

variable "db_instance_name" {
  description = "Cloud SQL instance name."
  type        = string
  default     = "aoe2-standings-db"
}

variable "db_name" {
  description = "Application database name (within the Cloud SQL instance)."
  type        = string
  default     = "aoe2_live_standings"
}

variable "db_user" {
  description = "Application database user."
  type        = string
  default     = "aoe2_app"
}

variable "tracked_profile_ids" {
  description = "One-time seed roster for an empty database: comma-separated Relic profile IDs used to bootstrap the first tournament. Read only by ensure_seed_tournament() at startup — a no-op once any tournament exists. The live roster is the tournament_players table, managed via the write API; this value does not track it."
  type        = string
  default     = "199325,347269"
}

variable "cors_origins" {
  description = "Comma-separated CORS allowlist for the deployed (production) service. Consumer dev origins live here so a developer running `hera-streamer-invitational-2026-web` locally can call the live preview API from their browser. The deployed consumer URL gets added once that service ships."
  type        = string
  default     = "http://localhost:5173"
}

variable "github_repository" {
  description = "The `owner/repo` GitHub Actions CD runs from. Scopes the Workload Identity provider's attribute_condition so only this repo's OIDC tokens can deploy."
  type        = string
  default     = "ag-tech-group/aoe2-live-standings-api"
}

variable "alerting_email" {
  description = "Email address that receives Cloud Monitoring alerts for the polling worker. Created as an unverified email channel — Google sends a verification link to this address on the first `tofu apply`; click it before alerts route, otherwise the channel sits idle. Override per-environment via -var or terraform.tfvars."
  type        = string
  default     = "amr@agtechgroup.solutions"
}

variable "billing_account" {
  description = "GCP billing account ID linked to this project, in the form `01234A-567890-BCDEF1`. Required at apply time for the budget alert; find it with `gcloud billing projects describe aoe2-live-standings-api --account=… --format='value(billingAccountName)'` (returns `billingAccounts/<id>` — the trailing id alone is what goes here). No default: it's neither secret nor inferable, and an apply-time error is preferable to a stale baked-in value drifting from reality."
  type        = string
}

variable "budget_monthly_usd" {
  description = "Monthly budget cap for the project in USD. Notification thresholds fire at 50%, 90%, and 100% of this amount on actual spend, plus a 100%-forecast warning when the month's projected total would exceed the cap. $100 default sits well above the steady-state idle cost (Cloud Run min-instances + Cloud SQL db-f1-micro ≈ a few USD/month), so the 50% trigger ($50) is the first signal of anything materially abnormal."
  type        = number
  default     = 100
}

variable "sentry_dsn" {
  description = "Sentry project DSN for unhandled-exception + log-error capture. Empty string (the default) disables Sentry entirely — `init_sentry()` in app code is a no-op, and no `SENTRY_DSN` env var is set on the Cloud Run services. Supply at apply time once the operator creates the Sentry project (`tofu apply -var sentry_dsn='https://…@…ingest.sentry.io/…'`). Sensitive=true so Terraform redacts it from plan output (the DSN is quasi-public — Sentry's docs encourage embedding it in client-side code — but redaction in logs is still tidier)."
  type        = string
  default     = ""
  sensitive   = true
}
