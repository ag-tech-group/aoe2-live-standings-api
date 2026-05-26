# Cloud SQL Postgres 16 — preview tier (db-f1-micro, shared-CPU, ~$8/mo)
# normally; bumped to db-g1-small (shared-CPU, 1.7GB, ~$33/mo) for the
# duration of the Hera invitational per #84.
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
    # cheap shared-core `db-f1-micro` / `db-g1-small` tiers we want for
    # preview and event-window operation.
    edition = "ENTERPRISE"
    # db-g1-small (1.7GB, shared-CPU) — raised from db-f1-micro (614MB)
    # for #84 event-window hardening. With Cloudflare absorbing polling
    # traffic the binding DB constraint is no longer reads/sec but
    # connection-pool memory under sustained pressure; the larger RAM
    # absorbs that and leaves headroom for the planner cache. Tier
    # change triggers a one-time instance restart — schedule outside
    # the event window. Revert to db-f1-micro after the event.
    tier              = "db-g1-small"
    availability_type = "ZONAL"
    disk_size         = 10
    disk_type         = "PD_HDD"

    backup_configuration {
      enabled = true
      # WAL archiving — lets us recover to any point inside the retention
      # window (after a bad migration or an accidental DELETE), not just to
      # the last nightly snapshot. Enabling it triggers a one-time instance
      # restart.
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      ipv4_enabled = true
      # No authorized networks listed — connections happen via Cloud SQL Auth
      # Proxy (IAM-authenticated) or via the Unix socket inside Cloud Run.
    }

    insights_config {
      query_insights_enabled = true
    }

    # db-f1-micro's default is ~25 connections (with a few reserved
    # for superusers), too tight for our scaling profile: api at
    # `max=10` × 5 pool + worker × 5 + migrate job + brief overlap
    # during rolling deploys can push past 25 transiently and starve
    # the migrate job. 100 gives headroom; Cloud SQL's own docs list
    # 100 as a recommended ceiling for the f1-micro tier (each
    # connection costs memory and the instance has only 614 MB).
    database_flags {
      name  = "max_connections"
      value = "100"
    }
  }

  # GCP-side guard: the Cloud SQL API rejects an instance delete while this
  # is true. Pairs with the Terraform-side prevent_destroy below — the two
  # block deletion at different layers (the cloud API vs. `tofu` planning a
  # destroy in the first place).
  deletion_protection = true

  lifecycle {
    prevent_destroy = true
  }
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
