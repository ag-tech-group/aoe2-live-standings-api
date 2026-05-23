# Silent-failure alerting for the polling worker.
#
# Cloud Run's built-in health checks won't catch a wedged poller — the
# worker container stays "healthy" with its HTTP server up even if the
# poller tasks have died. We need an outside-the-process watchdog.
#
# Two failure modes exist, but only one is handled here:
#
#   1. Loud — a poll tick raises, the run loop emits `poll_<task>_failed`,
#      catches the exception, and retries on the next cycle. **Sentry's
#      job** (planned). The exception itself is the signal; Sentry will
#      capture it with stack/breadcrumbs/fingerprinting far better than a
#      log-based threshold alert can.
#   2. Silent — the worker emits *neither* ok nor failed: a task died,
#      the instance wedged, the process is stuck. Default Sentry can't
#      see what didn't happen (only Sentry's paid Cron Monitoring would,
#      via per-tick heartbeat instrumentation). **Covered here**, via
#      Cloud Monitoring's `condition_absent` on per-task `poll_<task>_ok`
#      counters.
#
# This file therefore defines three log-based counters (one per task) and
# one alert policy reading them. Each task's absence-duration is sized at
# 5–8× its cadence to absorb container restarts and brief upstream blips
# without false-firing. Notifications go to a single email channel — swap
# in Slack/PagerDuty by adding more `google_monitoring_notification_channel`
# resources and extending the policy's `notification_channels` list.

locals {
  # Each entry: the task's poll cadence (informational, drives the
  # absence-duration choice) and the duration after which an absence
  # of `poll_<task>_ok` should fire the silent-failure alert.
  poller_tasks = {
    live_matches = {
      cadence_seconds  = 15
      absence_duration = "120s" # 8× cadence — duration must be a whole-minute multiple per Cloud Monitoring
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

# Bridge the freshly-created metrics into a queryable state before the
# alert policy references them. Cloud Monitoring takes up to a few
# minutes to make a new log-based metric resolvable by `metric.type=`,
# and `google_monitoring_alert_policy` validates that filter at create
# time — so on a clean apply, the policy races the metrics and fails
# with 404. This wait runs only on the *first* apply (when this
# resource itself is being created); subsequent applies skip it.
resource "time_sleep" "wait_for_metric_propagation" {
  depends_on      = [google_logging_metric.poll_ok]
  create_duration = "180s"
}

# Email notification channel. Created in unverified state — Google
# sends a verification code to the address on first apply; submit it
# via the `notificationChannels:verify` API (or the Cloud Console
# Monitoring → Alerting → Notification channels UI) before alerts
# route. An unverified channel won't error the apply but also won't
# deliver.
resource "google_monitoring_notification_channel" "email" {
  display_name = "Polling worker alerts (email)"
  type         = "email"
  labels = {
    email_address = var.alerting_email
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

  depends_on = [time_sleep.wait_for_metric_propagation]

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
