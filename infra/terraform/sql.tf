# Cloud SQL Postgres 16 — preview tier (db-f1-micro, shared-CPU, ~$8/mo).
#
# Connectivity to Cloud Run is via the built-in Cloud SQL Auth Proxy
# (mounted as a Unix socket at /cloudsql/<connection_name>); no VPC peering
# or private IP required for preview.

resource "google_sql_database_instance" "main" {
  name             = var.db_instance_name
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    # ENTERPRISE_PLUS is the default in recent provider versions and only
    # supports `db-perf-optimized-N-*` tiers; ENTERPRISE is what allows the
    # cheap shared-core `db-f1-micro` we want for preview.
    edition           = "ENTERPRISE"
    tier              = "db-f1-micro"
    availability_type = "ZONAL"
    disk_size         = 10
    disk_type         = "PD_HDD"

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = false # WAL archiving is overkill for preview
    }

    ip_configuration {
      ipv4_enabled = true
      # No authorized networks listed — connections happen via Cloud SQL Auth
      # Proxy (IAM-authenticated) or via the Unix socket inside Cloud Run.
    }

    insights_config {
      query_insights_enabled = true
    }
  }

  # Preview env — flip to true before any production traffic.
  deletion_protection = false
}

resource "google_sql_database" "app" {
  name     = var.db_name
  instance = google_sql_database_instance.main.name
}

# Random app-user password, persisted to Terraform state (encrypted at rest
# in the GCS backend) and reused for the DATABASE_URL secret. Rotating
# requires `tofu taint random_password.db_user` + apply + redeploy.
resource "random_password" "db_user" {
  length  = 32
  special = false # asyncpg URL-quoting is easier without symbols
}

resource "google_sql_user" "app" {
  name     = var.db_user
  instance = google_sql_database_instance.main.name
  password = random_password.db_user.result
}
