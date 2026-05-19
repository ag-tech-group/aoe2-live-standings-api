# Service account the Cloud Run service runs as.
#
# Granted just enough to: (a) reach the Cloud SQL instance via the auth
# proxy, and (b) read the DATABASE_URL secret at startup. Nothing else —
# this account doesn't deploy code, doesn't read other projects, doesn't
# touch storage.

resource "google_service_account" "cloud_run" {
  account_id   = "aoe2-standings-run"
  display_name = "AoE2 Live Standings API — Cloud Run runtime"
}

resource "google_project_iam_member" "cloud_run_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.cloud_run.email}"
}

resource "google_project_iam_member" "cloud_run_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.cloud_run.email}"
}
