variable "event_mode" {
  description = <<-EOT
    Operating posture for the whole stack, so activating for the next event is one
    variable flip instead of a hand-edit sweep. "active": SQL instance provisioned,
    api public with a warm min instance, worker polling, every alert policy enabled,
    uptime checks created. "dormant": SQL instance DELETED (a stopped instance of
    this shape still floors at ~$70/mo — see sql.tf), api internal-only at zero
    instances, worker paused, all alert policies disabled, uptime checks deleted.
    Flipping dormant -> active recreates the SQL instance but NOT its data — follow
    the resurrection runbook in sql.tf (instance-name reuse is blocked ~1 week after
    deletion; import the archived dump) before relying on the stack. Deliberately
    NOT covered by this switch: capacity sizing (maxScale, tiers, pool sizes) and
    the ZONAL -> REGIONAL finals lever in sql.tf — those stay explicit edits.
  EOT
  type        = string
  default     = "dormant"

  validation {
    condition     = contains(["active", "dormant"], var.event_mode)
    error_message = "event_mode must be \"active\" or \"dormant\"."
  }
}

variable "sse_seats_alert_threshold" {
  description = "Total concurrent SSE subscribers (summed across api instances) above which the seat-leak / capacity-pressure alert (monitoring.tf) fires. CALIBRATION PLACEHOLDER: retune to ~1.5x the observed host-live peak once a real broadcast has been measured via the sse_subscriber_count metric (#194). The 10,000 default is ~45% of the maxScale x concurrency seat ceiling — above the streamer-grind baseline, below a genuine marquee peak."
  type        = number
  default     = 10000
}

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

variable "twitch_client_id" {
  description = "Twitch application Client ID for broadcast-live detection (#112). Not a secret — Twitch exposes it in client-side calls — so it lives here as a committed default, like cors_origins. Its sensitive counterpart, the Client Secret, is deliberately NOT a variable: it lives in the `twitch-client-secret` Secret Manager secret and is referenced by Cloud Run via secret_key_ref (same pattern as sentry-dsn, see secrets.tf), so an apply can't silently wipe it. Empty disables detection."
  type        = string
  default     = "5le2v95z52178pcmqqkq1p59moevqo"
}

# `sentry_dsn` variable removed — the DSN now lives in Secret Manager
# (see `data "google_secret_manager_secret" "sentry_dsn"` in secrets.tf)
# and is referenced by Cloud Run via `secret_key_ref` in run.tf. This
# closes #91: a `tofu apply` without `-var sentry_dsn=…` no longer
# silently removes the SENTRY_DSN env var from production. Rotation
# happens by adding a new version to the secret; no TF apply needed.
