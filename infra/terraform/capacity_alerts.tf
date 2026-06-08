# Cloud SQL + Cloud Run capacity alerts.
#
# Threshold-based alert policies that fire as we approach the engineered
# ceilings rather than after we've crashed through them. Each routes to the
# Sentry-bound notification channel defined in alerts_sentry.tf so the operator
# sees them in the same triage surface as the app's exceptions.
#
# Originally scoped to the Hera invitational event window (#84), but the
# platform is now always-on (continuous streamer-ladder traffic, peaks when the
# host is live), so these are permanent — not something to revert when a single
# event closes. Post-cutover state (#196 / #255):
#   - The Cloud SQL policies were retargeted from the old `main` instance to
#     `main_v2` (Enterprise Plus / db-perf-optimized-N-2 / Managed Connection
#     Pooling) and retuned for the MCP profile. Under MCP, `num_backends` no
#     longer scales with Cloud Run instance count — the transaction pooler
#     (`max_pool_size = 50`, shared by api + worker) bounds it — so the real
#     ceiling is the 50-connection pool, NOT `max_connections = 400`, and a
#     backend climb signals pooler saturation or a direct-connection leak, not
#     organic app load.
#   - The Cloud Run concurrency policy was rescaled for the maxScale 20→40 bump
#     (#195).
# Thresholds are tuned for "warning, look at it," not "page someone."

# --- Cloud SQL CPU pressure ----------------------------------------------
#
# main_v2 is db-perf-optimized-N-2 (dedicated 2 vCPU). Sustained CPU > 70% over
# 5 minutes means the cores are saturating and queries will queue. With
# Cloudflare absorbing polling traffic and the pooler smoothing connection
# churn this should fire rarely; if it does, the next step is to scale up the
# Enterprise Plus perf-optimized tier (db-perf-optimized-N-4) or chase the
# offending query pattern in Query Insights.

resource "google_monitoring_alert_policy" "sql_cpu_high" {
  display_name = "Cloud SQL CPU > 70% (main_v2)"
  combiner     = "OR"
  severity     = "WARNING"

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "CPU > 70% sustained 5 minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"cloudsql.googleapis.com/database/cpu/utilization\"",
        "resource.type=\"cloudsql_database\"",
        "resource.labels.database_id=\"${var.project_id}:${google_sql_database_instance.main_v2.name}\"",
      ])
      duration        = "300s" # 5 minutes (whole-minute required)
      comparison      = "COMPARISON_GT"
      threshold_value = 0.7 # 70% as fraction

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  documentation {
    content   = "Cloud SQL CPU sustained above 70% for 5 minutes on main_v2 (db-perf-optimized-N-2, dedicated 2 vCPU). Cloudflare proxy should be absorbing polling traffic — if this fires, check (a) is the proxy still proxying (not bypassed?), (b) is the connection pool steady (`cloudsql.googleapis.com/database/postgresql/num_backends`), (c) any unusual query patterns in Query Insights. Next step if sustained: scale up the Enterprise Plus perf-optimized tier (db-perf-optimized-N-4)."
    mime_type = "text/markdown"
  }
}

# --- Cloud SQL active connections ----------------------------------------
#
# main_v2 has `max_connections = 400`, but the real ceiling is the MCP
# transaction pooler's `max_pool_size = 50` (shared across api + worker, which
# both connect through the pooler with `DB_USE_CONNECTOR=true`). Steady-state
# `num_backends` is ~4–20. So the two policies map to the two MCP failure modes:
#
#   - Early (45 backends, 1 min) — the pooler is ~90% of its 50-connection
#     pool. App requests will start queuing for a pooled connection. This is
#     the real capacity signal now (was "% of the 100-connection cap"); the fix
#     is raising `max_pool_size` / adding a pooler node, not the DB cap. The
#     1-minute window is tight on purpose — the 2026-06-01 outage's lesson was
#     that by the time 5-minute pressure trips, users already feel it (see
#     `[[project_cloud_run_revision_outage]]`).
#   - Critical (80 backends, 5 min) — well ABOVE the 50-connection pooler
#     ceiling, so the pooler can't be the source: something is opening backends
#     outside it (a service with `DB_USE_CONNECTOR=false` hitting the direct
#     socket, a stuck manual session, a pooler/connector misconfig). Still far
#     short of the 400 cap, so this is a "find the leak" signal, not "the DB is
#     about to refuse connections."
#
# NOTE: bracketed around the max_pool_size=50 ceiling, not an observed
# Hera-stream peak — main_v2 is fresh post-cutover. Revisit the early-warning
# number once we've seen a host-live peak on main_v2 (#255).

resource "google_monitoring_alert_policy" "sql_connections_climbing" {
  display_name = "Cloud SQL active connections > 45 (pooler saturating)"
  combiner     = "OR"
  severity     = "WARNING"

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "num_backends > 45 sustained 1 minute"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"cloudsql.googleapis.com/database/postgresql/num_backends\"",
        "resource.type=\"cloudsql_database\"",
        "resource.labels.database_id=\"${var.project_id}:${google_sql_database_instance.main_v2.name}\"",
      ])
      duration        = "60s"
      comparison      = "COMPARISON_GT"
      threshold_value = 45

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  documentation {
    content   = "main_v2 is holding > 45 active backends — the MCP transaction pooler (`max_pool_size = 50`, shared by api + worker) is ~90% full, so app requests may start queuing for a pooled connection. This is connection-pool pressure, not the 400-connection DB cap (still far off). Likely cause: a genuine traffic ramp (host live / finals) pushing concurrent transactions toward the pool ceiling. If sustained, the lever is raising `connection_pool_config.max_pool_size` (or adding a pooler node) on main_v2 — NOT the Cloud Run pool size. If it climbs past 80, the critical policy fires (that's a leak, not load)."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "sql_connections_high" {
  display_name = "Cloud SQL active connections > 80 (leak / pooler bypass)"
  combiner     = "OR"
  severity     = "CRITICAL"

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "num_backends > 80 sustained 5 minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"cloudsql.googleapis.com/database/postgresql/num_backends\"",
        "resource.type=\"cloudsql_database\"",
        "resource.labels.database_id=\"${var.project_id}:${google_sql_database_instance.main_v2.name}\"",
      ])
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 80

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  documentation {
    content   = "main_v2 is holding > 80 active backends (`num_backends`) — well above the MCP pooler's 50-connection ceiling, so the pooler is NOT the source. Something is opening backends outside it: confirm both api + worker still have `DB_USE_CONNECTOR=true` (a service on the direct socket bypasses the pooler), look for a stuck manual `psql`/proxy session or a runaway transaction in Query Insights, and check `connection_pool_config` didn't drift. Still short of the 400 `max_connections` cap, so this is a 'find the leak' signal. Last-resort reset (clears all backends, brief blip): `gcloud sql instances restart aoe2-standings-db-v2`."
    mime_type = "text/markdown"
  }
}

# --- Cloud Run per-instance concurrent requests --------------------------
#
# Each api instance is sized for 800 concurrent requests
# (`max_instance_request_concurrency = 800`). We alert on the active-instance
# count climbing to 80% of the maxScale=40 ceiling (#195) — 32 instances. Above
# this, Cloud Run keeps autoscaling toward 40, but the headroom is shrinking.
# Post-MCP the DB is no longer the constraint on scaling (the pooler decouples
# num_backends from instance count), so the lever here is raising `maxScale` /
# `max_instance_request_concurrency`, not a DB change.

resource "google_monitoring_alert_policy" "run_concurrency_high" {
  display_name = "Cloud Run api active instances > 32 (80% of maxScale=40)"
  combiner     = "OR"
  severity     = "WARNING"

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "max(active instances) > 32 sustained 5 minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"run.googleapis.com/container/instance_count\"",
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=\"${google_cloud_run_v2_service.api.name}\"",
      ])
      # instance_count is per-state (idle/active); we alert on active-instance
      # count reaching 32 — 80% of the 40-instance maxScale ceiling (#195).
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 32

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  documentation {
    content   = "The api service has more than 32 active Cloud Run instances (80% of the maxScale=40 ceiling, #195). At 800 concurrent requests/instance that's roughly 25,000+ concurrent SSE streams. If this fires, check (a) is real traffic this high (genuine host-live / finals ramp) or is something looping/retrying, (b) SSE seat utilization on the Cloud Run console. Since MCP decoupled DB connections from instance count, the next levers are raising `max_instance_count` or `max_instance_request_concurrency` — the DB pooler is no longer the blocker it was pre-#196."
    mime_type = "text/markdown"
  }
}
