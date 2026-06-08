# Cloud Run Job that runs Alembic migrations against Cloud SQL.
#
# Why a separate Job rather than `alembic upgrade head` in start.sh:
#
#   1. Avoids the race where a new revision rolls out while the old
#      one is still serving traffic — without coordination, both want
#      to migrate, and Alembic's per-statement locking only helps with
#      short DDL.
#   2. Makes migration failure a *hard* deploy halt: CI sees a non-zero
#      exit and stops before any service revision rolls. The
#      `start.sh` path leaks failure as a soft "container never
#      becomes Ready" signal that has to be noticed after the fact.
#   3. With two services post-#47 (api + worker), two containers wanted
#      to migrate independently; the Job consolidates that to one
#      place.
#
# The job uses the same image as the services (built once per merge in
# CI) and the same runtime SA + Cloud SQL volume + DATABASE_URL
# secret. The only difference from a service is the container
# `command`, which overrides `CMD ["./start.sh"]` to run alembic and
# exit.

resource "google_cloud_run_v2_job" "migrate" {
  name     = "${var.service_name}-migrate"
  location = var.region

  template {
    # `task_count = 1` and `parallelism = 1` are the defaults; one
    # migration task per execution. Alembic is not parallel-safe and
    # the migration set is small, so there's no reason to fan out.
    template {
      service_account = google_service_account.cloud_run.email

      # If a migration fails, don't auto-retry — surface the error.
      # The same migration applied twice could mask a real schema bug
      # behind transient flakiness.
      max_retries = 1

      # 10-minute cap. Typical schema-additive migrations are sub-
      # second; a 10-minute timeout is generous headroom for a one-off
      # data backfill that exceeds the norm. If something legitimately
      # needs longer, this needs revisiting (and probably a different
      # rollout strategy).
      timeout = "600s"

      containers {
        # Placeholder image — same dance as the services in run.tf.
        # Terraform creates the job shell with the cloudrun/hello
        # image so the resource exists before CI has ever pushed.
        # `ignore_changes` on image (below) stops Terraform from
        # racing CI back to the placeholder on subsequent applies.
        image = "us-docker.pkg.dev/cloudrun/container/hello:latest"

        # Override the Dockerfile's `CMD ["./start.sh"]`. The migrate
        # job doesn't need uvicorn — just alembic, then exit.
        command = ["uv", "run", "alembic", "upgrade", "head"]

        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.database_url.secret_id
              version = "latest"
            }
          }
        }

        # `ENVIRONMENT=production` so `Settings.validate_production_settings`
        # runs (it currently rejects a default `postgres:postgres@` DB
        # URL in non-dev). Migrations using prod credentials should
        # behave as prod everywhere else, too.
        env {
          name  = "ENVIRONMENT"
          value = "production"
        }

        volume_mounts {
          mount_path = "/cloudsql"
          name       = "cloudsql"
        }
      }

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          # Both sockets (#196 cutover): the migrate job's DATABASE_URL now
          # targets main_v2, so it needs main_v2's socket mounted; main stays
          # for rollback symmetry with the services. Drop main once it's retired.
          instances = [
            google_sql_database_instance.main.connection_name,
            google_sql_database_instance.main_v2.connection_name,
          ]
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      # CI's `gcloud run jobs update --image=...:$GITHUB_SHA` swaps the
      # image on every merge — Terraform owns the job's shape but not
      # its current image.
      template[0].template[0].containers[0].image,
      # Every `gcloud run jobs ...` invocation (deploy, execute, update)
      # stamps these top-level fields on the resource. Without ignoring
      # them, each `tofu plan` shows a `client/client_version → null`
      # diff that Terraform wants to clear, and the next gcloud call
      # rewrites it — recurring no-op drift. Matches the same ignore
      # on the api/worker services in run.tf.
      client,
      client_version,
    ]
  }
}
