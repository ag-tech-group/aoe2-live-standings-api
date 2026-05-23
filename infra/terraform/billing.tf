# Monthly budget alert for this project's GCP spend.
#
# Informational, not enforcing — Cloud Billing budgets don't cut off
# services when the cap is reached. The notification path routes to
# the same email channel the polling-worker alerts use, so the same
# inbox sees everything.
#
# Four threshold rules cover the common cases:
#
#   - 50% (current spend) — first signal that spend is materially
#     above the idle baseline; arrives early enough that there's time
#     to investigate before any real money is committed.
#   - 90% (current spend)  — approaching the cap; act now.
#   - 100% (current spend) — cap reached; spend continues but you'll
#     know about it in real time.
#   - 100% (forecasted spend) — mid-month projection of the full
#     month's total exceeds the cap. Fires before any of the above
#     when a sudden uptick would lead to overrun by month-end.
#
# A hard-stop (e.g., a Cloud Function on the budget's pub/sub topic
# that disables billing on threshold breach) is deliberately *not*
# wired up here. For a live-tournament workload, a runaway-cost cap
# that takes the API offline is worse than the cost itself; the human
# loop is the right circuit-breaker for now.

resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "${var.project_id} — monthly"

  budget_filter {
    projects = ["projects/${var.project_id}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = var.budget_monthly_usd
    }
  }

  # Current-spend thresholds — fire as the bill accumulates.
  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }

  # Forecast threshold — fires when the month-end projection crosses
  # the cap, even if current spend hasn't yet. Catches sudden upticks
  # (e.g., a runaway query or a sudden traffic spike) before they
  # actually push the bill over.
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  all_updates_rule {
    monitoring_notification_channels = [google_monitoring_notification_channel.email.id]
    # disable_default_iam_recipients = false keeps the default
    # behaviour of also pinging billing admins; the explicit channel
    # is the near-realtime path.
    disable_default_iam_recipients = false
  }
}
