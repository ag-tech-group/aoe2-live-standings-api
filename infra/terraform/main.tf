# Provider + remote-state config for the AoE2 Live Standings API preview infra.
#
# State lives in a versioned GCS bucket so multiple developers (and CI, later)
# can converge on the same source of truth. Run `tofu init` once to wire the
# backend, then `tofu plan` / `tofu apply` from this directory.

terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11"
    }
  }

  backend "gcs" {
    bucket = "aoe2-live-standings-api-tfstate"
    prefix = "terraform/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Bill API quota usage to this project rather than ADC's default
  # (which is unset on a fresh `gcloud auth application-default login`).
  # The OrgPolicy API specifically rejects requests without a quota
  # project; rather than mutate ADC shared state via
  # `set-quota-project`, the provider declares it explicitly here.
  user_project_override = true
  billing_project       = var.project_id
}
