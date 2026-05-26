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
#      job**. The exception itself is the signal; Sentry will capture it
#      with stack/breadcrumbs/fingerprinting far better than a log-based
#      threshold alert can. `enable_logs=True` on the SDK init also ships
#      ERROR-level structlog entries (the `poll_<task>_failed` events) to
#      Sentry alongside the auto-captured exceptions.
#   2. Silent — the worker emits *neither* ok nor failed: a task died,
#      the instance wedged, the process is stuck. Default Sentry can't
#      see what didn't happen (only Sentry's paid Cron Monitoring would,
#      via per-tick heartbeat instrumentation). **Covered here**, via
#      Cloud Monitoring's `condition_absent` on per-task `poll_<task>_ok`
#      counters.
#
# This file therefore defines three log-based counters (one per task) and
# one alert policy reading them.
#
# Thresholds: each task's absence-duration is set to 10 min — well above
# the steady-state cadence (15s / 30s / 60s) so brief upstream blips,
# revision rollovers, and Cloud Run instance rotations don't false-fire,
# but tight enough that a genuinely wedged worker is caught within a
# couple of polling cycles. The original 5–8× cadence thresholds (120s
# / 180s / 300s) flooded the operator's inbox at preview-scale; widening
# to 10 min trades a longer detection window for a saner signal-to-noise
# ratio. Revisit if/when this carries production-grade SLOs.
#
# Notification destination: none — the policy fires into the Cloud
# Monitoring incidents UI only, no email/Slack/PagerDuty. At preview
# scale the email flood was worse than the missed-signal cost, and the
# loud-failure path (Sentry) already covers exceptions. Wire a channel
# back in (add `google_monitoring_notification_channel` resources and
# put their names in `notification_channels` on the policy below) when
# this graduates past preview, ideally to Slack/PagerDuty rather than
# email so on-call has snooze/triage UI.

locals {
  # Each entry: the task's poll cadence (informational only — the
  # absence-duration is uniform across tasks). Duration must be a
  # whole-minute multiple per Cloud Monitoring (10m = 600s = ✓).
  poller_tasks = {
    live_matches = {
      cadence_seconds  = 15
      absence_duration = "600s" # 40× cadence
    }
    player_stats = {
      cadence_seconds  = 30
      absence_duration = "600s" # 20× cadence
    }
    recent_matches = {
      cadence_seconds  = 60
      absence_duration = "600s" # 10× cadence
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
#
# Used by the uptime and budget alerts (see uptime.tf, billing.tf); the
# poller silent-failure policy below deliberately leaves
# `notification_channels` empty so its incidents stay UI-only — see
# the file header for the rationale.
resource "google_monitoring_notification_channel" "email" {
  display_name = "Polling worker alerts (email)"
  type         = "email"
  labels = {
    email_address = var.alerting_email
  }
}

# Silent failure: a poller stops emitting `poll_<task>_ok` for the
# task's absence_duration. Each task has its own condition so the
# incident in the Cloud Monitoring UI names which task went silent —
# diagnostically useful even with notifications muted.
resource "google_monitoring_alert_policy" "poller_stalled" {
  display_name = "Polling worker — stalled (silent)"
  combiner     = "OR"
  severity     = "CRITICAL"

  depends_on = [time_sleep.wait_for_metric_propagation]

  # No notification channels — see the file header for the rationale.
  # Incidents are visible in the Cloud Monitoring UI; add a channel
  # here when this graduates past preview.
  notification_channels = []

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
