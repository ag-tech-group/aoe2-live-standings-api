output "service_url" {
  description = "Cloud Run-assigned *.run.app URL. Custom domain (criticalbit.gg) is set up manually via Cloud Run domain mapping + Cloudflare CNAME."
  value       = google_cloud_run_v2_service.api.uri
}

output "db_connection_name" {
  description = "Cloud SQL instance connection name (project:region:instance). Pass to Cloud SQL Auth Proxy for local migration runs."
  value       = google_sql_database_instance.main_v2.connection_name
}

output "artifact_registry_repo" {
  description = "Fully-qualified Artifact Registry Docker repo. Push images as <this>/<image>:<tag>."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.main.repository_id}"
}

output "service_account_email" {
  description = "Email of the SA the Cloud Run service runs as."
  value       = google_service_account.cloud_run.email
}

output "cd_workload_identity_provider" {
  description = "Full resource name of the WIF provider — the `workload_identity_provider` input for the google-github-actions/auth step."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "cd_deployer_service_account" {
  description = "Email of the GitHub Actions CD deployer SA — the `service_account` input for the google-github-actions/auth step."
  value       = google_service_account.deployer.email
}
