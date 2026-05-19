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
}
