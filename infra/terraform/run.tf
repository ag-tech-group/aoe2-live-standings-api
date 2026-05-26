# Cloud Run services running the FastAPI app — split into two roles
# per issue #14:
#
# - **api** (this resource) — public, autoscaling read tier. Serves the
#   `/v1/*` REST endpoints and the SSE `/v1/stream`. Has the LISTEN/NOTIFY
#   listener running in its lifespan so SSE clients on each instance
#   receive nudges fanned out from the worker's writes
#   (`LISTENER_ENABLED=true`). No pollers (`POLLING_ENABLED=false`).
#   Scales horizontally with request traffic (`min=1 max=10` normally;
#   bumped to `max=20` for the Hera invitational per #84).
# - **worker** (the resource below) — private singleton poller. Runs the
#   three polling tasks against the upstream Relic API and writes to
#   Postgres, emitting `pg_notify` on commit. No public traffic; no
#   `allUsers` invoker. `min=max=1` because the pollers must be a
#   singleton — a second instance would duplicate every upstream call
#   and create double-write contention on the same DB rows.
#
# Both services run the SAME image (`Dockerfile`'s `start.sh` runs
# `alembic upgrade head` then `uvicorn app.main:app`); they are
# differentiated only by the env vars below. We bootstrap with the
# hello placeholder image so Terraform can create the service shells
# before any image is pushed. After the first `docker push`, deploys go
# via `gcloud run deploy --image ...` from CI — the `ignore_changes`
# on `image` stops Terraform from rolling back to the placeholder.

resource "google_cloud_run_v2_service" "api" {
  name     = var.service_name
  location = var.region

  template {
    service_account = google_service_account.cloud_run.email

    # SSE connections (`GET /v1/stream`) are long-lived. The timeout is the
    # hard ceiling before Cloud Run recycles a request; at the cap (3600s)
    # an idle stream lives ~1h, then EventSource reconnects transparently.
    # Normal REST handlers finish in ms — the high ceiling never bites them.
    timeout = "3600s"

    # Each open SSE connection holds a request slot for its whole lifetime.
    # The default concurrency of 80 would cap concurrent viewers per
    # instance at 80; 800 buys headroom on each instance, and the
    # `max_instance_count = 20` scaling below gives 16000 concurrent
    # streams' worth of capacity.
    #
    # Concurrency stays at 800 (not raised alongside the instance count)
    # to keep per-instance memory pressure off the hot path — see the
    # memory limit below; raising both at once was flagged as needing a
    # stress test in #84 and we chose the horizontal-scale route instead.
    max_instance_request_concurrency = 800

    scaling {
      # min=1 keeps a warm instance so the LISTEN/NOTIFY connection stays
      # open continuously. max=20 (raised from 10 for #84 event-window
      # hardening) gives 800 × 20 = 16,000 concurrent SSE seats — well
      # over the 9,000-viewer High-active projection in
      # docs/event-traffic-cost-model.md. Revert to 10 after the event.
      min_instance_count = 1
      max_instance_count = 20
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
          cpu = "1"
          # 1Gi (raised from 512Mi for #84 event-window hardening). At the
          # 800-concurrent-SSE design point, 512Mi was within budget but
          # left no margin for the Sentry SDK buffer, OTel span batcher,
          # and the LISTEN/NOTIFY listener's state to grow under sustained
          # load. The extra headroom costs ~$5–10/mo across scaled
          # instances at peak and removes a memory-pressure failure mode
          # we'd otherwise discover during the event. Revert to 512Mi
          # after the event.
          memory = "1Gi"
        }
        # The LISTEN connection is event-driven and needs CPU between
        # requests to deliver NOTIFY callbacks (and run its 30s ping).
        cpu_idle = false
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

      # Seed roster for a brand-new, empty database only — consumed once by
      # ensure_seed_tournament() at startup. The live roster lives in the
      # tournament_players table and is managed via the write API.
      env {
        name  = "TRACKED_PROFILE_IDS"
        value = var.tracked_profile_ids
      }

      env {
        name  = "ENVIRONMENT"
        value = "production"
      }

      # No pollers on the api service — those run on the worker.
      env {
        name  = "POLLING_ENABLED"
        value = "false"
      }

      # The api service runs the LISTEN/NOTIFY listener so SSE clients
      # connected to it receive nudges fanned out from the worker's
      # writes.
      env {
        name  = "LISTENER_ENABLED"
        value = "true"
      }

      env {
        name  = "CORS_ORIGINS"
        value = var.cors_origins
      }

      # Sentry DSN — pulled from Secret Manager via the data block in
      # secrets.tf (see that file for the seeding recipe and rotation
      # notes). Static block, not dynamic — the secret is the source of
      # truth. To disable Sentry entirely, delete every version of the
      # `sentry-dsn` secret; the next revision roll would then fail to
      # start (loud signal), not silently boot Sentry-less.
      env {
        name = "SENTRY_DSN"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.sentry_dsn.secret_id
            version = "latest"
          }
        }
      }

      # OpenTelemetry tracing → Cloud Trace (#58). Auth comes from the
      # runtime SA's roles/cloudtrace.agent binding; project is
      # inferred via metadata.
      env {
        name  = "OTEL_ENABLED"
        value = "true"
      }
      env {
        name  = "OTEL_USE_CLOUD_TRACE"
        value = "true"
      }
      env {
        name  = "OTEL_TRACES_SAMPLE_RATIO"
        value = "0.1"
      }
    }
  }

  lifecycle {
    ignore_changes = [
      # Image is updated out-of-band via `gcloud run deploy` after each
      # push (see the file-level comment above).
      template[0].containers[0].image,
      client,
      client_version,
      # GCP populates a top-level (service-level) `scaling` block as a
      # representation default. We manage scaling via `template.scaling`;
      # ignoring the service-level block stops a spurious "remove scaling"
      # diff on every plan.
      scaling,
    ]
  }

  depends_on = [
    google_project_iam_member.cloud_run_sql_client,
    google_project_iam_member.cloud_run_secret_accessor,
    google_secret_manager_secret_version.database_url,
  ]
}


resource "google_cloud_run_v2_service" "worker" {
  name     = "${var.service_name}-worker"
  location = var.region

  template {
    service_account = google_service_account.cloud_run.email

    scaling {
      # min=max=1 keeps the poller a strict singleton. A second instance
      # would duplicate every upstream call and double-write to the same
      # DB rows. Multi-instance polling needs leader-election / cron we
      # haven't built (and may never need at this scale).
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
        # Always-on CPU so the pollers' asyncio loops run continuously
        # between any internal /health probes (no incoming user traffic
        # otherwise — the worker is private).
        cpu_idle = false
      }

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

      # Seed roster for a brand-new, empty database only — consumed once by
      # ensure_seed_tournament() at startup. The live roster lives in the
      # tournament_players table and is managed via the write API.
      env {
        name  = "TRACKED_PROFILE_IDS"
        value = var.tracked_profile_ids
      }

      env {
        name  = "ENVIRONMENT"
        value = "production"
      }

      # Pollers live on the worker — the api service has them off.
      env {
        name  = "POLLING_ENABLED"
        value = "true"
      }

      # No listener on the worker — it has no SSE clients to fan out
      # nudges to. The worker's writes emit `pg_notify`s that the api
      # service's listener consumes.
      env {
        name  = "LISTENER_ENABLED"
        value = "false"
      }

      # Sentry DSN — same Secret Manager source as the api service. The
      # worker wants Sentry coverage too (a polling task raising). See
      # secrets.tf for the seeding + rotation recipe.
      env {
        name = "SENTRY_DSN"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.sentry_dsn.secret_id
            version = "latest"
          }
        }
      }

      # OpenTelemetry tracing — same config as the api service. The
      # worker's poll-tick spans are exported alongside the api's
      # request spans into the same Cloud Trace project.
      env {
        name  = "OTEL_ENABLED"
        value = "true"
      }
      env {
        name  = "OTEL_USE_CLOUD_TRACE"
        value = "true"
      }
      env {
        name  = "OTEL_TRACES_SAMPLE_RATIO"
        value = "0.1"
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
      scaling,
    ]
  }

  depends_on = [
    google_project_iam_member.cloud_run_sql_client,
    google_project_iam_member.cloud_run_secret_accessor,
    google_secret_manager_secret_version.database_url,
  ]
}


# Public access for the API service — preview-scale, no auth on reads.
# The worker is deliberately *not* listed here: no `allUsers` invoker,
# no public traffic. Cloud Run's internal probes don't need a public
# IAM binding.
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
