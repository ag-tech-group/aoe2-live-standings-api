# Cloud Run service running the FastAPI app + asyncio polling worker.
#
# Why min=1, max=1: this is a stateful service. The poller's asyncio
# tasks live inside the running process; autoscaling to zero would kill
# them, and a second instance would create duplicate polling + double-
# write contention on the same DB rows. v1 is single-worker; multi-
# instance polling needs a leader-election / cron design we haven't
# built yet (see follow-up #9 for related freshness work).
#
# We bootstrap with the hello placeholder image so Terraform can create
# the service shell before there's any pushed image. After the first
# `docker push`, deploy via `gcloud run deploy --image ...` — the
# `ignore_changes` on `image` keeps subsequent Terraform applies from
# rolling the deployed image back to the placeholder.

resource "google_cloud_run_v2_service" "api" {
  name     = var.service_name
  location = var.region

  template {
    service_account = google_service_account.cloud_run.email

    scaling {
      min_instance_count = 1
      max_instance_count = 1
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }

    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello:latest"

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = false # min-instance always-on CPU so the poller runs between requests
      }

      # Cloud Run auto-creates this mount when a `cloud_sql_instance` volume
      # is declared at the template level, but Terraform's idempotency
      # check wants it declared explicitly here — otherwise every plan
      # tries to delete the auto-created mount.
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.database_url.secret_id
            version = "latest"
          }
        }
      }

      env {
        name  = "TRACKED_PROFILE_IDS"
        value = var.tracked_profile_ids
      }

      env {
        name  = "ENVIRONMENT"
        value = "production"
      }

      env {
        name  = "POLLING_ENABLED"
        value = "true"
      }

      env {
        name  = "CORS_ORIGINS"
        value = var.cors_origins
      }
    }
  }

  # Image is updated out-of-band via `gcloud run deploy` after each push.
  # See the file-level comment above.
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_iam_member.cloud_run_sql_client,
    google_project_iam_member.cloud_run_secret_accessor,
    google_secret_manager_secret_version.database_url,
  ]
}

# Public access — preview-scale, no auth surface in v1. Locked down later
# if the API ever gates writes; reads are intentionally open since
# tournament streamers are the consumer.
#
# `depends_on` the org policy override: the agtechgroup.solutions org
# restricts IAM principals to the org's customer ID by default, which
# rejects `allUsers`. The project-level policy in org_policy.tf lifts
# that restriction just for this project.
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = google_cloud_run_v2_service.api.project
  location = google_cloud_run_v2_service.api.location
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"

  depends_on = [google_org_policy_policy.allow_public_iam]
}
