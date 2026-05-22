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
