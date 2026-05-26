# Sentry-routed alerting plumbing — Pub/Sub topic + Cloud Function +
# notification channel. Reusable across any Cloud Monitoring alert policy:
# attach the channel exported below to the policy's
# `notification_channels` and the incident lands in Sentry instead of
# email/Slack/PagerDuty.
#
# Why we built this: Cloud Monitoring has no native Sentry destination,
# and email had a real flood incident at preview-scale (see the header of
# monitoring.tf for the postmortem). A small Pub/Sub-triggered forwarder
# is the minimum that gets infra alerts into Sentry without coupling
# alert plumbing to the worker (which itself fires alerts when it stalls
# — routing through it would silence its own outage).
#
# Topology:
#
#   alert policy → notification_channel (type=pubsub)
#                      ↓
#                  Pub/Sub topic (cloud-monitoring-alerts)
#                      ↓
#                  Cloud Function (cloud-monitoring-to-sentry)
#                      ↓
#                  sentry_sdk.capture_message → Sentry
#
# The function source lives in `infra/functions/cloud-monitoring-to-sentry/`.
# This file zips it on `tofu plan`, uploads the zip to a GCS bucket
# keyed by content hash, and points a Cloud Functions 2nd gen resource at
# the object. Source changes trigger a content-hash change, which triggers
# a new bucket object, which triggers a function redeploy.
#
# Pre-apply requirements (one-time per project):
#   - Enable APIs: cloudfunctions, cloudbuild, eventarc, pubsub. Run:
#     gcloud --account=<email> --project=aoe2-live-standings-api \
#       services enable cloudfunctions.googleapis.com cloudbuild.googleapis.com \
#                       eventarc.googleapis.com pubsub.googleapis.com
#   - `sentry-dsn` secret seeded (see secrets.tf for the recipe).

# --- Pub/Sub topic for incident notifications -----------------------------

resource "google_pubsub_topic" "alerts" {
  name = "cloud-monitoring-alerts"
}

# The Cloud Monitoring service agent publishes to the topic when an
# alert policy attached to the pubsub channel fires. Without this
# binding the channel apply succeeds but no messages ever land.
data "google_project" "current" {
  project_id = var.project_id
}

resource "google_pubsub_topic_iam_member" "monitoring_publisher" {
  topic  = google_pubsub_topic.alerts.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
}

# --- Notification channel that alert policies attach to -------------------

resource "google_monitoring_notification_channel" "sentry_pubsub" {
  display_name = "Sentry (via Pub/Sub forwarder)"
  type         = "pubsub"
  labels = {
    topic = google_pubsub_topic.alerts.id
  }
  description = "Forwards Cloud Monitoring incidents to Sentry via the cloud-monitoring-to-sentry Cloud Function. See infra/functions/cloud-monitoring-to-sentry/README.md."

  depends_on = [google_pubsub_topic_iam_member.monitoring_publisher]
}

# --- Cloud Function service account + IAM ---------------------------------

resource "google_service_account" "fn_alert_forwarder" {
  account_id   = "fn-alert-to-sentry"
  display_name = "AoE2 — cloud-monitoring → Sentry forwarder"
  description  = "Runtime SA for the cloud-monitoring-to-sentry Cloud Function."
}

# Read the Sentry DSN secret. Scoped to the single secret, not
# project-wide, so a future bug in the function can't exfiltrate other
# secrets even if it tried.
resource "google_secret_manager_secret_iam_member" "fn_sentry_dsn_accessor" {
  secret_id = data.google_secret_manager_secret.sentry_dsn.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.fn_alert_forwarder.email}"
}

# Eventarc → Cloud Run plumbing under the hood of 2nd gen Pub/Sub
# triggers. Without this role the function deploys but never receives
# events.
resource "google_project_iam_member" "fn_eventarc_receiver" {
  project = var.project_id
  role    = "roles/eventarc.eventReceiver"
  member  = "serviceAccount:${google_service_account.fn_alert_forwarder.email}"
}

# Cloud Functions 2nd gen runs on Cloud Run; the underlying Cloud Run
# service needs the runtime SA to have `roles/run.invoker` to receive
# Eventarc events through the IAM check.
resource "google_project_iam_member" "fn_run_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.fn_alert_forwarder.email}"
}

# --- Function source: zip + upload ---------------------------------------

# GCS bucket for Cloud Function source archives. One bucket can hold
# many functions' sources keyed by object name; no need for per-function
# buckets.
resource "google_storage_bucket" "fn_sources" {
  name                        = "${var.project_id}-fn-sources"
  location                    = var.region
  uniform_bucket_level_access = true

  # Older source zips become unreferenced when a new content-hash
  # supersedes them. Keep the most recent two object generations as a
  # safety net (in case a deploy fails and we need to revert), prune the
  # rest after 30 days to control bucket sprawl.
  lifecycle_rule {
    condition {
      age                = 30
      num_newer_versions = 2
    }
    action {
      type = "Delete"
    }
  }
}

# Re-zip whenever any file under the function directory changes. The
# resulting MD5 becomes part of the object name so any source change
# triggers a new object and, transitively, a function redeploy.
data "archive_file" "fn_alert_forwarder_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../functions/cloud-monitoring-to-sentry"
  output_path = "${path.module}/.terraform-fn-cm-to-sentry.zip"
  excludes    = ["__pycache__", ".venv", "*.pyc"]
}

resource "google_storage_bucket_object" "fn_alert_forwarder_src" {
  name   = "cloud-monitoring-to-sentry/${data.archive_file.fn_alert_forwarder_zip.output_md5}.zip"
  bucket = google_storage_bucket.fn_sources.name
  source = data.archive_file.fn_alert_forwarder_zip.output_path
}

# --- Cloud Function (2nd gen, Python 3.12, Pub/Sub trigger) --------------

resource "google_cloudfunctions2_function" "alert_to_sentry" {
  name        = "cloud-monitoring-to-sentry"
  location    = var.region
  description = "Forwards Cloud Monitoring incident notifications to Sentry. See infra/functions/cloud-monitoring-to-sentry/README.md."

  build_config {
    runtime     = "python312"
    entry_point = "forward_alert"

    source {
      storage_source {
        bucket = google_storage_bucket.fn_sources.name
        object = google_storage_bucket_object.fn_alert_forwarder_src.name
      }
    }
  }

  service_config {
    # The forwarder is stateless and low-volume (one invocation per
    # incident edge). 256Mi is comfortably above what sentry-sdk +
    # functions-framework consume; the bottleneck is the outbound HTTP
    # to ingest.sentry.io, not memory.
    available_memory = "256Mi"
    timeout_seconds  = 30

    # Cold-start latency matters less than not piling up duplicate
    # forwards on a transient spike. Cap at 3.
    max_instance_count = 3
    min_instance_count = 0

    service_account_email = google_service_account.fn_alert_forwarder.email

    environment_variables = {
      ENVIRONMENT = "production"
    }

    # Same `sentry-dsn` Secret Manager secret the API + worker read
    # (see secrets.tf). Cloud Functions 2nd gen injects the secret
    # value as an env var at startup, matching how Cloud Run does it.
    secret_environment_variables {
      key        = "SENTRY_DSN"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.sentry_dsn.secret_id
      version    = "latest"
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic   = google_pubsub_topic.alerts.id
    # Don't retry: a malformed payload or a transient Sentry outage
    # shouldn't generate N duplicate Sentry events when the message
    # eventually goes through. The forwarder logs the failure either
    # way; Cloud Monitoring's UI still has the canonical incident.
    retry_policy = "RETRY_POLICY_DO_NOT_RETRY"
  }

  depends_on = [
    google_secret_manager_secret_iam_member.fn_sentry_dsn_accessor,
    google_project_iam_member.fn_eventarc_receiver,
    google_project_iam_member.fn_run_invoker,
  ]
}
