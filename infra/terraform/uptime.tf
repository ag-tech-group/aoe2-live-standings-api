# External synthetic monitoring of the read surface.
#
# Cloud Run's built-in liveness probe checks the container's HTTP
# endpoint — but a healthy container doesn't mean a healthy service.
# A regression that breaks `/v1/tournaments/{slug}/standings` returns
# 500 to users, while Cloud Run's probe of `/` still returns 200 — the
# service stays "healthy" while the actual product is broken. These
# uptime checks close that gap by hitting the real read paths from
# outside the deployment.
#
# Four checks cover the critical-path reads. Each runs every minute
# from multiple Google-managed regions; the policy alerts when 2+
# regions report a failure for ≥2 minutes. Routed to the same email
# channel the polling-worker alerts and budget notifications use.
#
# Volume sits well inside Cloud Monitoring's free tier (4 checks ×
# 60/hr × 6 regions ≈ 1500 calls/hr against a >1M/month limit).

locals {
  # Each check: a probed path and the substring the response body
  # must contain on success. Substring matching catches "the endpoint
  # is up but returns garbage" — a plain 200-status check would miss
  # an empty body or an HTML error page.
  uptime_endpoints = {
    root = {
      path             = "/"
      expected_content = "\"status\":\"ok\""
      description      = "Baseline service health"
    }
    tournament_list = {
      path             = "/v1/tournaments"
      expected_content = "\"slug\""
      description      = "DB read path"
    }
    standings = {
      path             = "/v1/tournaments/default/standings"
      expected_content = "\"items\""
      description      = "End-to-end worker→DB→api pipeline (standings depend on the worker writing)"
    }
    leaderboards = {
      path             = "/v1/leaderboards"
      expected_content = "\"items\""
      description      = "DB-backed leaderboards (post-#43; proves the cache table is populated)"
    }
  }

  # The custom-domain host. Cloud Run's `*.run.app` URL also works but
  # the criticalbit.gg host is what real users hit — checking it
  # transitively verifies Cloudflare + the domain mapping.
  uptime_host = "aoe2-live-standings-api.criticalbit.gg"
}

resource "google_monitoring_uptime_check_config" "endpoint" {
  for_each = local.uptime_endpoints

  display_name = "aoe2 — ${each.key}"

  timeout = "10s"
  period  = "60s"
  selected_regions = [
    "USA_OREGON",
    "USA_IOWA",
    "USA_VIRGINIA",
  ]

  http_check {
    path           = each.value.path
    port           = "443"
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }
  }

  content_matchers {
    content = each.value.expected_content
    matcher = "CONTAINS_STRING"
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      host       = local.uptime_host
      project_id = var.project_id
    }
  }
}

# Single alert policy spanning all four checks. Each check is its own
# condition; combiner=OR means any one's failure fires. Threshold of
# 0.6 over a 300s window with the standard uptime-check metric means
# "more than 60% of probes failed in the last 5 minutes" — generous
# enough to absorb a single-region transient blip, tight enough to
# catch a real outage within a couple of minutes.
resource "google_monitoring_alert_policy" "uptime" {
  display_name = "Uptime checks — read surface"
  combiner     = "OR"
  severity     = "CRITICAL"

  notification_channels = [google_monitoring_notification_channel.email.id]

  dynamic "conditions" {
    for_each = local.uptime_endpoints
    content {
      display_name = "${conditions.key} unreachable"

      condition_threshold {
        filter = join(" AND ", [
          "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\"",
          "resource.type=\"uptime_url\"",
          "metric.labels.check_id=\"${google_monitoring_uptime_check_config.endpoint[conditions.key].uptime_check_id}\"",
        ])

        # `check_passed` is a bool (1 = passed, 0 = failed). Fraction-
        # true aggregation gives the per-window pass rate; threshold
        # below `0.4` = more than 60% failure.
        duration        = "300s"
        comparison      = "COMPARISON_LT"
        threshold_value = 0.4

        aggregations {
          alignment_period     = "60s"
          per_series_aligner   = "ALIGN_FRACTION_TRUE"
          cross_series_reducer = "REDUCE_MEAN"
          group_by_fields      = ["resource.label.host"]
        }

        # Trigger when at least one of the cross-series (the regions)
        # crosses the threshold — defaults to "all" otherwise.
        trigger {
          count = 1
        }
      }
    }
  }

  documentation {
    content   = "An uptime check against the live read surface is failing in 2+ regions. Hit the endpoint manually to confirm; check Cloud Run revision status on `aoe2-live-standings-api` (the listener service); check Cloudflare for an upstream issue. The `standings` and `leaderboards` checks depend on the worker writing — a failure there could indicate the worker stalled (cross-reference the polling-worker stalled alert)."
    mime_type = "text/markdown"
  }
}
