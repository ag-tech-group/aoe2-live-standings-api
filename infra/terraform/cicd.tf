# Continuous deployment: GitHub Actions -> Cloud Run, keyless via
# Workload Identity Federation.
#
# No long-lived service-account JSON key lives in GitHub secrets. Instead,
# GitHub Actions presents its short-lived OIDC token; GCP's workload
# identity pool trusts that token (scoped by `attribute_condition` to this
# one repo) and lets it impersonate the `github-deployer` service account.

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-actions"
  display_name              = "GitHub Actions"
  description               = "Identity pool for GitHub Actions CD."
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  # Required by GCP on new providers — without a condition, *any* GitHub
  # repo's OIDC token could target this provider. Scope it to ours.
  attribute_condition = "assertion.repository == \"${var.github_repository}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# The identity GitHub Actions deploys as. Distinct from the Cloud Run
# *runtime* SA (`aoe2-standings-run`) — this one only builds + ships.
resource "google_service_account" "deployer" {
  account_id   = "github-deployer"
  display_name = "GitHub Actions CD deployer"
}

# Let OIDC tokens from this repo impersonate the deployer SA.
resource "google_service_account_iam_member" "deployer_workload_identity" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member = format(
    "principalSet://iam.googleapis.com/%s/attribute.repository/%s",
    google_iam_workload_identity_pool.github.name,
    var.github_repository,
  )
}

# Deploy Cloud Run revisions (developer, not admin — no IAM-policy power).
resource "google_project_iam_member" "deployer_run" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# Push images to Artifact Registry.
resource "google_project_iam_member" "deployer_artifact_registry" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# A Cloud Run deploy sets the service's runtime SA, which requires
# act-as permission on that SA — even when the runtime SA is unchanged.
resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  service_account_id = google_service_account.cloud_run.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}
