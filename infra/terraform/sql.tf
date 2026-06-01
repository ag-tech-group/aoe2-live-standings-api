# Cloud SQL Postgres 16 — dedicated-CPU tier `db-custom-1-3840`
# (1 vCPU dedicated, 3.75 GB RAM, ~$50/mo).
#
# Tier history:
# - Preview: `db-f1-micro` (shared-CPU, 614 MB, ~$8/mo).
# - 2026-05 event hardening (#84): `db-g1-small` (shared-CPU, 1.7 GB,
#   ~$33/mo) — bigger RAM to absorb the per-connection memory cost.
# - 2026-06-01 outage follow-up: `db-custom-1-3840` (this).
#   The g1-small was on the edge of its connection budget; combined
#   with the stale-revision proliferation it ran the DB out of slots
#   under load (see `[[project_cloud_run_revision_outage]]`). The
#   prune step (#171) + per-instance pool tightening (#172) plug the
#   leak; this tier bump adds (a) dedicated CPU so query latencies
#   don't share with noisy neighbours, (b) headroom to raise
#   `max_connections` past 100 if audience growth pushes maxScale
#   past 20, and (c) larger planner cache + working-memory headroom.
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
    # supports `db-perf-optimized-N-*` tiers; ENTERPRISE allows the
    # `db-custom-N-M` family we now use (and the cheaper shared-core
    # tiers we used previously).
    edition = "ENTERPRISE"
    # `db-custom-1-3840`: 1 vCPU (dedicated), 3.75 GB RAM. The smallest
    # dedicated-CPU tier — first step past shared-CPU's noisy-neighbour
    # behaviour. Tier change triggers a one-time instance restart on
    # apply; expect ~60-90s of DB unavailability. After the restart,
    # Cloud Run instances reconnect cleanly via `pool_pre_ping` and the
    # worker resumes polling on its own (we observed this end-to-end
    # during the 2026-06-01 outage's manual restart).
    tier              = "db-custom-1-3840"
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

    # 200 connections at the new tier. With 3.75 GB of RAM and ~10 MB
    # per connection, 200 conns ≈ 2 GB of connection memory — leaves
    # ~1.75 GB for shared buffers + planner cache + work mem, which is
    # the sustainable ceiling per Cloud SQL's per-tier guidance.
    #
    # Peak demand at the current scaling profile (api maxScale=20 ×
    # pool 3 + worker × 3 + migrate overlap) is ~68 connections, so
    # 200 leaves ~130 of headroom — enough to absorb an emergency
    # maxScale bump to ~40 without another DB change, and enough to
    # double-buffer a future PgBouncer rollout that would multiplex
    # many app conns onto fewer DB conns.
    database_flags {
      name  = "max_connections"
      value = "200"
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
