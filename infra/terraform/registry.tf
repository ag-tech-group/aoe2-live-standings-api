# Artifact Registry repo for Docker images.
#
# Push pattern (local laptop, for preview):
#   gcloud auth configure-docker us-central1-docker.pkg.dev --account=...
#   docker build -t us-central1-docker.pkg.dev/<project>/<repo>/<image>:<tag> .
#   docker push us-central1-docker.pkg.dev/<project>/<repo>/<image>:<tag>

resource "google_artifact_registry_repository" "main" {
  location      = var.region
  repository_id = var.service_name
  description   = "Docker images for the AoE2 Live Standings API"
  format        = "DOCKER"

  # Cost hygiene (2026-07-23): ~290 CI deploys had accumulated 9.5 GB of
  # superseded images (~$0.70/mo, growing without bound). Keep the 10 newest
  # versions — the CI revision-prune step keeps only 2 servable Cloud Run
  # revisions, so 10 is ample rollback depth — and delete anything older than
  # 30 days beyond those. KEEP rules always win over DELETE rules.
  cleanup_policies {
    id     = "keep-recent-10"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  cleanup_policies {
    id     = "delete-stale"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
    }
  }
}
