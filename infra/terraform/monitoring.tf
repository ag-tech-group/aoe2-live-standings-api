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
  # absence-duration is per-task, since the broadcast pollers run at
  # very different cadences than the main three). Duration must be a
  # whole-minute multiple per Cloud Monitoring.
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
    twitch_live = {
      cadence_seconds  = 60
      absence_duration = "600s" # 10× cadence
    }
    # YouTube polls at 30-min cadence (quota-bound — see
    # app/poller/live_streams.py:_YOUTUBE_INTERVAL_SECONDS). Tighten this
    # only if more YouTube-only players land on the roster and the
    # 90-min absence buffer becomes uncomfortable; 3× cadence absorbs the
    # occasional skipped tick without false-firing.
    youtube_live = {
      cadence_seconds  = 1800
      absence_duration = "5400s" # 3× cadence
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

  # Mirror of worker_log_filter for the read (api) tier — the SSE hub and
  # its subscriber-count samples live on the api service, so its metric is
  # locked to the api service the same way the poller metrics lock to the
  # worker.
  api_log_filter = join(" AND ", [
    "resource.type=\"cloud_run_revision\"",
    "resource.labels.service_name=\"${google_cloud_run_v2_service.api.name}\"",
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

# Live SSE subscriber gauge (#194). The api tier's binding resource is the
# request slots held open by long-lived SSE streams, not throughput or DB
# load — but until now we could only *infer* concurrent seats from Cloud
# Run instance scaling. The api app samples `hub.subscriber_count` every
# 30s and logs it as `sse_subscriber_count`; this metric extracts that
# value into a per-instance distribution so finals capacity planning reads
# the real number instead of guessing. No alert hangs off it (a planning
# signal, not a paging condition), so it's intentionally left out of the
# wait_for_metric_propagation bridge below.
resource "google_logging_metric" "sse_subscriber_count" {
  name        = "sse_subscriber_count"
  description = "Concurrent SSE subscribers held open on each api instance (sampled every 30s)."
  filter      = "${local.api_log_filter} AND jsonPayload.event=\"sse_subscriber_count\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "1"
  }

  value_extractor = "EXTRACT(jsonPayload.count)"

  bucket_options {
    explicit_buckets {
      # Seats per instance: 0 at idle up past the 800-concurrency cap, with
      # finer buckets low (most instances hold tens–hundreds) and a 1200
      # ceiling matching the #197 concurrency target.
      bounds = [0, 1, 2, 5, 10, 25, 50, 100, 200, 400, 800, 1200]
    }
  }
}

# Dead-tab leak / SSE-seat early-warning (#204). With the FE EventSource
# cleanup + `timeout=600` (#204) and the halved read-tier pool (#206), the
# live `sse_subscriber_count` should track real concurrent viewers
# (cross-check PostHog active users). A sustained TOTAL far above that —
# summed across api instances — means either a dead-tab regression (FE
# cleanup or the SSE timeout broke) or genuine capacity pressure climbing
# toward the maxScale × concurrency seat ceiling (~22,000). Either is worth
# a look, routed to the same Sentry surface.
#
# THRESHOLD IS A PLACEHOLDER. We don't yet know real peak (peak = a Hera
# live stream). Tune to ~1.5× the observed peak once #194 reveals the real
# concurrent-seat number during a Hera broadcast. 10,000 is ~45% of the 22k
# ceiling — above the streamer-grind baseline but below a genuine marquee
# peak; revisit after the next match.
resource "google_monitoring_alert_policy" "sse_seat_leak" {
  display_name = "SSE seats high — dead-tab leak or capacity pressure (#204)"
  combiner     = "OR"
  severity     = "WARNING"

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "total SSE subscribers > 10,000 sustained 5 minutes"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.sse_subscriber_count.name}\" AND resource.type=\"cloud_run_revision\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 10000

      # Each api instance logs its current seat count; ALIGN_MEAN gives that
      # instance's level per window, REDUCE_SUM totals across instances.
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MEAN"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    content   = "Total concurrent SSE subscribers (summed across api instances) has been > 10,000 for 5 minutes. With the FE EventSource cleanup + the 600s SSE timeout (#204) this should track real viewers — cross-check PostHog active users. If it's far above real viewers → **dead-tab regression**: confirm the FE still closes EventSource on pagehide/visibilitychange and that the api `timeout` is still 600s (infra/terraform/run.tf). If real viewers really are this high → **genuine capacity pressure**: watch num_backends and consider the maxScale / Cloud SQL tier levers (#195). Threshold is a placeholder — tune to ~1.5× the observed Hera-stream peak."
    mime_type = "text/markdown"
  }
}

# Bridge freshly-created metrics into a queryable state before any
# alert policy references them. Cloud Monitoring takes up to a few
# minutes to make a new log-based metric resolvable by `metric.type=`,
# and `google_monitoring_alert_policy` validates that filter at create
# time — so on a clean apply, the policy races the metrics and fails
# with 404. The `triggers` block re-runs the sleep whenever the
# universe of metrics changes (new poller task added, or a new
# sibling metric like `upstream_rate_limited`), so a "just adding one
# more metric" apply gets the same propagation window as the first
# apply did.
resource "time_sleep" "wait_for_metric_propagation" {
  depends_on = [
    google_logging_metric.poll_ok,
    google_logging_metric.upstream_rate_limited,
  ]
  create_duration = "180s"

  triggers = {
    metric_set = join(",", concat(
      keys(google_logging_metric.poll_ok),
      [google_logging_metric.upstream_rate_limited.name],
    ))
  }
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

  # Routed to Sentry Pub/Sub since the 2026-06-01 outage: the worker
  # stalled silently for 30+ minutes during a DB connection exhaustion
  # event, the UI-only alert presumably fired but nobody was watching
  # the Monitoring console. Loud failure → Sentry is the right
  # surface; the original preview-scale rationale for keeping this
  # quiet (the file-header note above) doesn't survive a live event.
  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

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

# Upstream worldsedgelink rate-limit signal (#120). The poller's HTTP
# client logs a distinct `upstream_rate_limited` event on every 429
# response; this metric counts them, the alert fires when they sustain.
#
# Routed to the Sentry channel — this is an actionable infra signal
# (knob to adjust: poll cadence or upstream coordination), not a
# silent-failure absence, so it doesn't share the UI-only treatment of
# the silent-failure alert above.
resource "google_logging_metric" "upstream_rate_limited" {
  name        = "upstream_rate_limited"
  description = "Upstream worldsedgelink 429 responses logged by the worker poller."
  filter      = "${local.worker_log_filter} AND jsonPayload.event=\"upstream_rate_limited\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "upstream_rate_limited" {
  display_name = "Upstream worldsedgelink — sustained rate-limit responses"
  combiner     = "OR"
  severity     = "WARNING"

  depends_on = [time_sleep.wait_for_metric_propagation]

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "upstream_rate_limited > 5 in any 3-min window"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.upstream_rate_limited.name}\" AND resource.type=\"cloud_run_revision\""
      duration        = "180s" # 3 minutes (whole-minute required)
      comparison      = "COMPARISON_GT"
      threshold_value = 5

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  documentation {
    content   = "The worker's HTTP client is getting 429s from worldsedgelink at a sustained rate (>5 in the last 3 minutes). A transient burst is absorbed by the window; sustained 429s typically mean (a) upstream tightened their rate limit, (b) our poll cadence is too aggressive for the current roster size, or (c) a runaway retry loop. Check the per-task interval constants in `app/poller/*` and the HTTP client config in `app/poller/client.py`. If 429s persist across a cadence reduction, coordinate with the upstream operator."
    mime_type = "text/markdown"
  }
}

# API-side 429 rate-limit storm (#188). The viewer-facing complement to
# the upstream_rate_limited signal above: that one catches the *worker*
# being throttled by worldsedgelink; this one catches *our* API throwing
# 429s back at viewers.
#
# Why this needs its own alert rather than riding Sentry: a 429 is a
# handled response, not an exception. slowapi's stock
# `_rate_limit_exceeded_handler` (registered in app/main.py) returns it
# without raising or logging, and app/sentry.py sets no
# `failed_request_status_codes`, so the SDK default captures only 5xx. A
# 429 storm therefore produces zero Sentry issues — which is exactly why
# the 2026-06-01 storm (#176) was silent. "Rate of a response code" is an
# SLI; Cloud Run's built-in request_count metric is the right tool, and
# routing it through the same sentry_pubsub channel keeps every infra
# alert in one triage surface.
#
# Permanent, not event-window: a CDN-fronted limiter/cache
# misconfiguration is a structural failure mode, not a Hera-specific one,
# so this lives here with the other permanent alerts rather than in the
# event-window-disposable capacity_alerts.tf that #188 first proposed.
resource "google_monitoring_alert_policy" "api_429_storm" {
  display_name = "API 429 rate-limit storm (viewers being rejected)"
  combiner     = "OR"
  severity     = "CRITICAL"

  notification_channels = [google_monitoring_notification_channel.sentry_pubsub.id]

  conditions {
    display_name = "429 responses > 20/min sustained 2 minutes"
    condition_threshold {
      filter = join(" AND ", [
        "metric.type=\"run.googleapis.com/request_count\"",
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=\"${google_cloud_run_v2_service.api.name}\"",
        "metric.labels.response_code=\"429\"",
      ])
      duration        = "120s" # 2 minutes (whole-minute required)
      comparison      = "COMPARISON_GT"
      threshold_value = 20 # 429s per 60s window; normal is ~0

      # request_count is a DELTA metric split per-revision. ALIGN_DELTA
      # yields the 429 count in each 60s window; REDUCE_SUM collapses the
      # per-revision series so a storm spread across two revisions during
      # a deploy rollover still sums to the true total. NB: ALIGN_RATE
      # would give a per-*second* rate — threshold 20 would then mean
      # ~1200/min and silently miss the "hundreds/min" storm this guards.
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    content   = "The api service is returning 429s to viewers at storm rate (>20/min sustained 2 minutes; steady-state is ~0). Viewers are being rate-limited at the edge. Check (a) `cf-cache-status` is HIT under load — a cache-miss storm means the Cloudflare cache rule (#178) regressed or `Vary: Cookie` crept back; (b) the limiter keys on `CF-Connecting-IP`, not the Cloudflare edge IP (#176) — otherwise every viewer shares one bucket; (c) Cloud Run isn't refusing requests upstream of the limiter. Background: docs/launch-lessons-learned.md (429 storm) and the 2026-06-01 incident."
    mime_type = "text/markdown"
  }
}
