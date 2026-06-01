# Event-window capacity alerts for the Hera invitational (#84).
#
# Three threshold-based alert policies that fire when we're approaching
# the engineered ceilings rather than after we've crashed through them.
# Each one routes to the Sentry-bound notification channel defined in
# alerts_sentry.tf so the operator sees them in the same triage surface
# as the app's exceptions.
#
# Thresholds are tuned for "warning, look at it" not "page someone." The
# capacity bumps in #84 (max_instances 10→20, db tier f1-micro→g1-small,
# memory 512Mi→1Gi) intentionally over-provision so these alerts mostly
# tell us whether the headroom was needed, not that we have to react in
# real time.
#
# All three are temporary — they target event-window pressure points
# specifically. Revert this file (delete or restore the original
# thresholds) when the event window closes. The Sentry routing
# infrastructure in alerts_sentry.tf is permanent and stays.

# --- Cloud SQL CPU pressure ----------------------------------------------
#
# db-g1-small is shared-CPU. Sustained CPU > 70% over 5 minutes means
# we're saturating the shared core and queries will queue. With
# Cloudflare absorbing polling traffic this should fire rarely or
# never; if it does fire, the next step is to bump to a dedicated-CPU
# tier (db-custom-1-3840 or similar).

resource "google_monitoring_alert_policy" "sql_cpu_high" {
  display_name = "Cloud SQL CPU > 70% (#84 event-window)"
  combiner     = "OR"
  severity     = "WARNING"

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "CPU > 70% sustained 5 minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"cloudsql.googleapis.com/database/cpu/utilization\"",
        "resource.type=\"cloudsql_database\"",
        "resource.labels.database_id=\"${var.project_id}:${google_sql_database_instance.main.name}\"",
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
    content   = "Cloud SQL CPU sustained above 70% for 5 minutes on the g1-small event-window tier. Cloudflare proxy should be absorbing polling traffic — if this fires, check (a) is the proxy still proxying (not bypassed?), (b) is the connection pool steady (`cloudsql.googleapis.com/database/postgresql/num_backends`), (c) any unusual query patterns in Query Insights. Next step if sustained: bump to a dedicated-CPU tier."
    mime_type = "text/markdown"
  }
}

# --- Cloud SQL active connections ----------------------------------------
#
# `max_connections` is 100 on the SQL instance. Two policies fire:
#
#   - Early (60 backends, 1 min) — heads-up that the pool is climbing
#     past steady-state. Steady-state is < 30 connections (api × pool
#     + worker + migrate-job overlap), so 60 is a real change but well
#     before refusals begin. The 1-minute window is tight because by
#     the time 5-minute sustained pressure trips, the saturation event
#     is already visible to users (this is the 2026-06-01 outage's
#     bitter lesson; see `[[project_cloud_run_revision_outage]]`).
#   - Critical (80 backends, 5 min) — 80% of the cap, sustained.
#     Refusals start at 97 (~3 superuser reserves). Keeping the
#     longer window on the critical policy keeps it as the "things
#     are bad now" signal and avoids double-firing on a brief spike
#     that the early-warning already covered.

resource "google_monitoring_alert_policy" "sql_connections_climbing" {
  display_name = "Cloud SQL active connections > 60 (early warning)"
  combiner     = "OR"
  severity     = "WARNING"

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "num_backends > 60 sustained 1 minute"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"cloudsql.googleapis.com/database/postgresql/num_backends\"",
        "resource.type=\"cloudsql_database\"",
        "resource.labels.database_id=\"${var.project_id}:${google_sql_database_instance.main.name}\"",
      ])
      duration        = "60s"
      comparison      = "COMPARISON_GT"
      threshold_value = 60

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  documentation {
    content   = "Cloud SQL is holding > 60 active backends — well above steady-state (< 30). This is the heads-up before the 80-backend critical fires. Likely causes: (a) stale Cloud Run revisions accumulating with `minScale=1` pinning instances + pools — check `gcloud run revisions list` on both services and confirm the CI prune step ran on the most recent deploy; (b) genuine traffic ramp pushing api instances past their pool budget; (c) a long-running transaction holding slots. Going past 80 fires the critical policy; past 97 the DB refuses connections."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "sql_connections_high" {
  display_name = "Cloud SQL active connections > 80 (critical)"
  combiner     = "OR"
  severity     = "CRITICAL"

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "num_backends > 80 sustained 5 minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"cloudsql.googleapis.com/database/postgresql/num_backends\"",
        "resource.type=\"cloudsql_database\"",
        "resource.labels.database_id=\"${var.project_id}:${google_sql_database_instance.main.name}\"",
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
    content   = "Cloud SQL is holding > 80 active backends (`num_backends`) — 80% of the 100 `max_connections` cap. Steady-state for this service is < 30; sustained 80+ means the pool is saturating. Check Cloud Run instance counts (api scaling above expected? worker spawning extras?), pgbouncer-style pooling absence, or stuck transactions in Query Insights. Going past 97 results in connection refusals. The 2026-06-01 outage cleared via `gcloud sql instances restart aoe2-standings-db` once the leak source (stale revision proliferation) was identified."
    mime_type = "text/markdown"
  }
}

# --- Cloud Run per-instance concurrent requests --------------------------
#
# Each api instance is sized for 800 concurrent requests
# (`max_instance_request_concurrency = 800`); 720 is 90% of that ceiling.
# Above this, Cloud Run autoscales up to a new instance, but during the
# scale-up window incoming requests queue. Sustained per-instance
# saturation tells us we're scaling, not just spiky — useful diagnostic
# even with `max_instances=20` headroom.

resource "google_monitoring_alert_policy" "run_concurrency_high" {
  display_name = "Cloud Run api concurrent requests > 720/instance (#84 event-window)"
  combiner     = "OR"
  severity     = "WARNING"

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "max(instance concurrency) > 720 sustained 5 minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"run.googleapis.com/container/instance_count\"",
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=\"${google_cloud_run_v2_service.api.name}\"",
      ])
      # The instance_count metric is per-state (idle/active). We alert
      # on active-instance count climbing — 16 active instances out of
      # the 20-max ceiling is the same 80%-of-cap signal expressed
      # horizontally rather than per-instance.
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 16

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  documentation {
    content   = "The api service has more than 16 active Cloud Run instances (80% of the event-window 20-instance cap). At 800 concurrent requests/instance this is roughly 12,800+ concurrent SSE streams; the High-active projection in docs/event-traffic-cost-model.md is 9,000. If this fires, check (a) is real traffic this high (genuine event ramp) or is something looping/retrying, (b) the SSE seat utilization on Cloud Run console — if individual instances are near the 800 cap, raising max_instances or max_instance_request_concurrency are the next levers."
    mime_type = "text/markdown"
  }
}
