# Observability + alerting for the polling worker.
#
# Two failure modes need coverage, neither caught by Cloud Run's built-in
# health checks (the worker container stays "healthy" with the HTTP server
# up even if the poller tasks are dead):
#
#   1. Loud — a poll tick raises and the run loop emits `poll_<task>_failed`,
#      then catches the exception and retries on the next cycle. The worker
#      stays running but data goes stale. Caught by counting failed events.
#   2. Silent — the worker emits *neither* ok nor failed: a task died, the
#      instance wedged, the process is stuck. Caught only by the *absence*
#      of `poll_<task>_ok` for longer than the task's own cadence.
#
# Six log-based counters (3 ok + 3 failed, one pair per task) feed two
# alert policies. Each task's absence-duration is sized at 5–6× its
# cadence to absorb container restarts and brief upstream blips without
# false-firing. Notifications go to a single email channel — swap in
# Slack/PagerDuty by adding more `google_monitoring_notification_channel`
# resources and extending `notification_channels` on each policy.

locals {
  # Each entry: the task's poll cadence (informational, drives the
  # absence-duration choice) and the duration after which an absence
  # of `poll_<task>_ok` should fire the silent-failure alert.
  poller_tasks = {
    live_matches = {
      cadence_seconds  = 15
      absence_duration = "90s" # 6× cadence
    }
    player_stats = {
      cadence_seconds  = 30
      absence_duration = "180s" # 6× cadence
    }
    recent_matches = {
      cadence_seconds  = 60
      absence_duration = "300s" # 5× cadence
    }
  }

  # Shared log-entry prefix for every poller metric. Locking it to the
  # worker service prevents stray emissions from a misconfigured api
  # instance (POLLING_ENABLED accidentally true) from polluting the
  # counters or masking a real stall on the worker.
  worker_log_filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.worker.name}\"",
  ])
}

# Successful-tick counters — one per task. The absence-of-data alert
# reads these to detect a stalled worker.
resource "google_logging_metric" "poll_ok" {
  for_each = local.poller_tasks

  name        = "poll_${each.key}_ok"
  description = "Successful ${each.key} poll cycles on the worker."
  filter      = "${local.worker_log_filter} AND jsonPayload.event=\"poll_${each.key}_ok\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

# Failed-tick counters — one per task. The threshold alert reads these
# to detect recurring errors (single transient failures are expected
# and retried on the next tick — only repeated ones get attention).
resource "google_logging_metric" "poll_failed" {
  for_each = local.poller_tasks

  name        = "poll_${each.key}_failed"
  description = "Failed ${each.key} poll cycles on the worker."
  filter      = "${local.worker_log_filter} AND jsonPayload.event=\"poll_${each.key}_failed\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

# Email notification channel. Created in unverified state — Google
# sends a verification link to the address on first apply; click it
# before alerts route. (An unverified channel won't error the apply
# but also won't deliver.)
resource "google_monitoring_notification_channel" "email" {
  display_name = "Polling worker alerts (email)"
  type         = "email"
  labels = {
    email_address = var.alerting_email
  }
}

# Loud failure: any single task crosses 2+ failures in a 5-minute
# rolling window. Threshold `> 1` over `ALIGN_SUM`/300s = the events
# during that window, requiring `duration = 60s` of sustained breach
# to suppress single-tick aberrations.
resource "google_monitoring_alert_policy" "poller_erroring" {
  display_name = "Polling worker — erroring (loud)"
  combiner     = "OR"
  severity     = "WARNING"

  notification_channels = [google_monitoring_notification_channel.email.name]

  dynamic "conditions" {
    for_each = local.poller_tasks
    content {
      display_name = "${conditions.key} failed >= 2 in 5min"
      condition_threshold {
        filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.poll_failed[conditions.key].name}\" AND resource.type=\"cloud_run_revision\""
        duration        = "60s"
        comparison      = "COMPARISON_GT"
        threshold_value = 1 # strict GT: fires on 2 or more events
        aggregations {
          alignment_period   = "300s"
          per_series_aligner = "ALIGN_SUM"
        }
      }
    }
  }

  documentation {
    content   = "The polling worker is logging `poll_*_failed` events at >= 2/5min. The enclosing run loop retries on the next tick, so this is not a service outage, but data is going stale. Inspect Cloud Run logs on `aoe2-live-standings-api-worker` for the failure messages — most often an upstream aoe2-api schema or rate-limit change."
    mime_type = "text/markdown"
  }
}

# Silent failure: a poller stops emitting `poll_<task>_ok` for longer
# than its cadence + margin. Each task has its own threshold so a
# wedged `live_matches` poller (15s cadence) is caught much sooner
# than a stalled `recent_matches` poller (60s cadence).
resource "google_monitoring_alert_policy" "poller_stalled" {
  display_name = "Polling worker — stalled (silent)"
  combiner     = "OR"
  severity     = "CRITICAL"

  notification_channels = [google_monitoring_notification_channel.email.name]

  dynamic "conditions" {
    for_each = local.poller_tasks
    content {
      display_name = "${conditions.key} ok absent for ${conditions.value.absence_duration}"
      condition_absent {
        filter   = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.poll_ok[conditions.key].name}\" AND resource.type=\"cloud_run_revision\""
        duration = conditions.value.absence_duration
        aggregations {
          alignment_period   = "60s"
          per_series_aligner = "ALIGN_SUM"
        }
      }
    }
  }

  documentation {
    content   = "The polling worker hasn't emitted a `poll_<task>_ok` event within the expected cadence window. The poller task may have died, the Cloud Run instance may be wedged, or the process may be otherwise stuck. Check Cloud Run revision status on `aoe2-live-standings-api-worker`; a forced revision roll (push a new image, or `gcloud run services update --update-env-vars=DUMMY=now`) typically clears wedges. If absence persists across a fresh revision, the bug is in the poller code."
    mime_type = "text/markdown"
  }
}
