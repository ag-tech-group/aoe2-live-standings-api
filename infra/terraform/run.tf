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
    # hard ceiling before Cloud Run recycles a request; EventSource then
    # reconnects transparently. Dropped 3600s -> 600s: behind Cloudflare +
    # Cloud Run a closed browser tab's disconnect often isn't propagated to
    # the origin (the proxy keeps the upstream warm, so the handler's
    # `is_disconnected()` never fires), so a dead tab's stream lingers — and
    # each lingering stream pins a Cloud Run instance, which holds a DB pool,
    # inflating num_backends (observed: ~30 real users but 150+ backends).
    # A 10-min ceiling caps that leak — dead tabs recycle in <=10min instead
    # of <=1h, so idle instances scale down and release pools far sooner.
    # Live viewers just reconnect every 10min (transparent; the nudge refetch
    # is CDN-coalesced). Normal REST handlers finish in ms — never bitten.
    timeout = "600s"

    # Each open SSE connection holds a request slot for its whole lifetime.
    # The default concurrency of 80 would cap concurrent viewers per
    # instance at 80; 1000 (with the `max_instance_count = 22` scaling
    # below) gives 22,000 concurrent streams' worth of capacity.
    #
    # Raised 800 -> 1000 in #197. 1000 is Cloud Run's hard per-instance
    # concurrency ceiling — the issue's 1200 target is rejected by the API
    # ("must be between 0 and 1000"), so 1000 is as far as this lever goes.
    # Unlike adding instances, concurrency adds NO database connections
    # (those scale with instance count x pool, not with concurrency), so
    # it's the cheap seat-multiplier once memory holds — the lever the 1Gi
    # limit below was provisioned for in #84. Memory at 1000/instance under
    # sustained SSE load is validated by a monitored rollout rather than a
    # synthetic test: the `sse_subscriber_count` metric (#194) plus
    # per-instance memory utilization are watched at the next live match,
    # with a revert to 800 ready if either climbs unsafe.
    max_instance_request_concurrency = 1000

    scaling {
      # min=1 keeps a warm instance for the nudge poll loop. max=100 (raised
      # from 40 on 2026-06-16) with concurrency=1000 gives 100,000 concurrent
      # SSE seats' worth of capacity. Rationale: the 2026-06-16 ladder-race
      # close (Phase 1) peaked ONE revision at 35/40 instances (~88% of the old
      # ceiling) carrying all traffic; the Phase 2 finals (Jun 18-21) — a live
      # grand-finals cast, typically a bigger draw than a ladder grind — left
      # too little headroom at 40. 100 covers well over 2x that observed peak
      # (~2.8x), the conservative choice for an unpredictable finals draw.
      #
      # DB-safe regardless of instance count: all api DB access goes through
      # Managed Connection Pooling (#196), so num_backends is bounded by the
      # pooler's server pool (max_pool_size=50, sql.tf), NOT instances x pool —
      # it does not scale with maxScale or with deploy-flurry revision overlap.
      # Nudges are polled via nudge_versions (no LISTEN), so there's no per-
      # instance direct connection either. (Pre-pooling this was capped at 22 —
      # peak backends ~= 8*maxScale + 6 would have blown past the old cap; that
      # ceiling is gone, and max_connections is now 400.) NOTE: the pooler's
      # max_pool_size=50 is the next axis to validate for 2x finals load — the
      # query-concurrency ceiling, distinct from this instance/connection one.
      min_instance_count = 1
      max_instance_count = 100
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        # main_v2's socket (the live instance). `main` was dropped here when it
        # was retired (#254) — it had been kept only as a no-re-mount rollback
        # target during the #196 cutover soak.
        instances = [
          google_sql_database_instance.main_v2.connection_name,
        ]
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
        # cpu_idle=false so the nudge poll loop (and the connector's pooled
        # connections) keep ticking between requests, not only during them.
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

      env {
        name  = "ENVIRONMENT"
        value = "production"
      }

      # No pollers on the api service — those run on the worker.
      env {
        name  = "POLLING_ENABLED"
        value = "false"
      }

      # The api service runs the nudge poller (poll_for_nudges) so SSE clients
      # connected to it refetch when the worker advances nudge_versions.
      env {
        name  = "LISTENER_ENABLED"
        value = "true"
      }

      # Cloud SQL connector → Managed Connection Pooling (#196). The whole api
      # DB footprint — request queries AND the nudge poll — goes through the
      # connector to main_v2's transaction pooler (app/database.py). No LISTEN /
      # direct connection anywhere. Rollback: DB_USE_CONNECTOR=false + repoint
      # DATABASE_URL back to main (both sockets stay mounted, so no re-mount).
      env {
        name  = "DB_USE_CONNECTOR"
        value = "true"
      }
      env {
        name  = "DB_INSTANCE_CONNECTION_NAME"
        value = google_sql_database_instance.main_v2.connection_name
      }
      env {
        name  = "DB_USER"
        value = var.db_user
      }
      env {
        name  = "DB_NAME"
        value = var.db_name
      }
      env {
        name = "DB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_app_password.secret_id
            version = "latest"
          }
        }
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
        # main_v2's socket (the live instance). `main` was dropped here when it
        # was retired (#254) — it had been kept only as a no-re-mount rollback
        # target during the #196 cutover soak.
        instances = [
          google_sql_database_instance.main_v2.connection_name,
        ]
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

      env {
        name  = "ENVIRONMENT"
        value = "production"
      }

      # Pollers live on the worker — the api service has them off.
      env {
        name  = "POLLING_ENABLED"
        value = "true"
      }

      # No nudge poller on the worker — it has no SSE clients. Its writes bump
      # nudge_versions, which the api instances' pollers pick up.
      env {
        name  = "LISTENER_ENABLED"
        value = "false"
      }

      # Cloud SQL connector → MCP pooler (#196). The worker's writes (and the
      # nudge_versions bump) go through the connector with the statement-cache
      # flags — under MCP the /cloudsql socket also routes to the transaction
      # pooler, so a raw socket connection isn't safe for the worker either.
      env {
        name  = "DB_USE_CONNECTOR"
        value = "true"
      }
      env {
        name  = "DB_INSTANCE_CONNECTION_NAME"
        value = google_sql_database_instance.main_v2.connection_name
      }
      env {
        name  = "DB_USER"
        value = var.db_user
      }
      env {
        name  = "DB_NAME"
        value = var.db_name
      }
      env {
        name = "DB_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_app_password.secret_id
            version = "latest"
          }
        }
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

      # Broadcast-live detection (#112) — Twitch + YouTube poller
      # credentials. Worker only: the api service reads results from the DB,
      # never calls Twitch/YouTube. The Client ID is non-secret (a var); the
      # two secrets come from Secret Manager (see secrets.tf). Each poller is
      # a no-op until its credential is set, so an unset key disables only
      # that platform.
      env {
        name  = "TWITCH_CLIENT_ID"
        value = var.twitch_client_id
      }
      env {
        name = "TWITCH_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.twitch_client_secret.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "YOUTUBE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.youtube_api_key.secret_id
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
