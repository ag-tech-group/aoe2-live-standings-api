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

# --- Enterprise Plus + Managed Connection Pooling instance (#196) ------------
#
# The "green" instance for the blue-green migration off `main`. We can't
# upgrade `main` in place: Enterprise Plus on the N2 series requires PD_SSD,
# `main` is on PD_HDD, and a Cloud SQL instance's disk type is IMMUTABLE
# (HDD->SSD forces a recreate, which `prevent_destroy` blocks). So we stand up
# a fresh EP/SSD/MCP instance alongside `main`, migrate data (export/import —
# the only non-reconstructable data is the small tournament-config tables; the
# polled tables refill from upstream within a poll cycle), then cut DATABASE_URL
# + the connector envs over to it. `main` stays as the rollback target until the
# new instance has soaked.
#
# Managed Connection Pooling (`connection_pool_config`) is Enterprise-Plus-only
# and is what decouples `num_backends` from Cloud Run instance count: the api
# request engine connects through the connector to the transaction pooler
# (app/database.py, DB_USE_CONNECTOR), so many app connections multiplex onto a
# few server backends. The LISTEN/NOTIFY listener and the Alembic migrate job
# stay on the DIRECT unix socket (transaction pooling drops LISTEN + advisory
# locks) — see secrets.tf (database-url) and app/events.py.
resource "google_sql_database_instance" "main_v2" {
  name             = "${var.db_instance_name}-v2"
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    edition = "ENTERPRISE_PLUS"
    # Smallest Enterprise Plus / N2 tier: 2 vCPU, 16 GB. EP/N2 mandates SSD.
    tier      = "db-perf-optimized-N-2"
    disk_type = "PD_SSD"
    disk_size = 10
    # REGIONAL: synchronous standby in a second zone for HA (~2x DB cost),
    # chosen for finals uptime insurance. Combined with EP's near-zero-downtime
    # maintenance, planned ops and zonal failures stay invisible to viewers.
    availability_type = "REGIONAL"

    # Enterprise Plus data cache: extends the buffer pool onto local SSD. A
    # read-latency win for this read-heavy workload, included with EP.
    data_cache_config {
      data_cache_enabled = true
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      ipv4_enabled = true
    }

    insights_config {
      query_insights_enabled = true
    }

    # Managed Connection Pooling, transaction mode. The pooler holds a bounded
    # set of server connections (sized by max_pool_size) and multiplexes all
    # client connections onto them — so api instance count no longer drives
    # num_backends. Reserve >=15 server conns/vCPU for the pooler (>=30 here);
    # max_connections=400 leaves ample room for the pooler pool + the per-
    # instance direct LISTEN connections + the worker. Tune max_pool_size from
    # the validation run (clone/staged) before raising maxScale (#195).
    connection_pool_config {
      connection_pooling_enabled = true
      flags {
        name  = "pool_mode"
        value = "transaction"
      }
      flags {
        name  = "max_pool_size"
        value = "50"
      }
    }

    database_flags {
      name  = "max_connections"
      value = "400"
    }
  }

  deletion_protection = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_sql_database" "app_v2" {
  name     = var.db_name
  instance = google_sql_database_instance.main_v2.name
}

# Reuse the same generated app password as `main` so the DATABASE_URL secret
# format works against either instance and the connector path shares one
# credential — simplifies cutover and rollback.
resource "google_sql_user" "app_v2" {
  name     = var.db_user
  instance = google_sql_database_instance.main_v2.name
  password = random_password.db_user.result
}
