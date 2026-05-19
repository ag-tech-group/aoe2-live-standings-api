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
  description = "Comma-separated Relic profile IDs the poller watches. Empty list short-circuits the per-profile pollers. Tournament rosters land here."
  type        = string
  default     = "199325,347269"
}

variable "cors_origins" {
  description = "Comma-separated CORS allowlist. The consumer's deployed domain goes here. Leave blank in dev and the app falls back to localhost:5100-5199."
  type        = string
  default     = ""
}
