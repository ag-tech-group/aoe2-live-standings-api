# Cloud SQL Postgres 16.
#
# main_v2 (below) is the live instance: Enterprise Plus / db-perf-optimized-N-2
# / PD_SSD / REGIONAL with Managed Connection Pooling. The original `main`
# instance (ENTERPRISE / db-custom-1-3840 / PD_HDD / ZONAL) was retired in #254
# after the #196 blue-green cutover — main_v2 had soaked carrying all traffic
# while `main` sat idle. Connectivity to Cloud Run is via the Cloud SQL Python
# connector to the transaction pooler (the pooled request engine, app/database.py)
# and the built-in Auth Proxy Unix socket at /cloudsql/<connection_name> (the
# Alembic migrate job).

# Random app-user password, persisted to Terraform state (encrypted at rest in
# the GCS backend) and shared by main_v2's app user and both DB secrets
# (database-url, db-app-password). Rotating requires
# `tofu taint random_password.db_user` + apply + redeploy.
resource "random_password" "db_user" {
  length  = 32
  special = false # asyncpg URL-quoting is easier without symbols
}

# --- Enterprise Plus + Managed Connection Pooling instance (#196) ------------
#
# The "green" instance from the blue-green migration off the original `main`
# (retired in #254). EP on the N2 series requires PD_SSD and a Cloud SQL disk
# type is IMMUTABLE (HDD->SSD forces a recreate), so rather than upgrade `main`
# in place we stood up this fresh EP/SSD/MCP instance, migrated data
# (export/import — the only non-reconstructable data is the small
# tournament-config tables; the polled tables refill from upstream within a poll
# cycle), then cut DATABASE_URL + the connector envs over to it.
#
# Managed Connection Pooling (`connection_pool_config`) is Enterprise-Plus-only
# and is what decouples `num_backends` from Cloud Run instance count: the api
# request engine connects through the connector to the transaction pooler
# (app/database.py, DB_USE_CONNECTOR), so many app connections multiplex onto a
# few server backends. The Alembic migrate job stays on the DIRECT unix socket
# (transaction pooling drops advisory locks) — see secrets.tf (database-url).
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
    # ZONAL for the post-event dormant period. REGIONAL's synchronous standby
    # in a second zone (~2x DB compute) was finals uptime insurance; with the
    # King's Gauntlet rated window closed (2026-06-16), the poller paused, and
    # the instance serving only frozen-standings reads (~600 visitors/day, <5%
    # CPU), HA buys nothing. This is an in-place update (no recreate, EP +
    # Managed Connection Pooling preserved); apply triggers a brief failover
    # blip. Restore "REGIONAL" before the next event's finals.
    availability_type = "ZONAL"

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
    # instance direct LISTEN connections + the worker. Raised 50->100 on
    # 2026-06-16 from observed prod peak (not a staged run): the ladder-race
    # close ran 27 active server conns / 50 with ~0 client wait, so a 2x finals
    # (~54 active) would exceed 50; 100 leaves clean headroom, still << 400.
    connection_pool_config {
      connection_pooling_enabled = true
      flags {
        name  = "pool_mode"
        value = "transaction"
      }
      flags {
        name  = "max_pool_size"
        value = "100"
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

# Reuse the same generated app password (random_password.db_user, formerly also
# the retired `main`'s user) so the DATABASE_URL secret format and the connector
# path share one credential.
resource "google_sql_user" "app_v2" {
  name     = var.db_user
  instance = google_sql_database_instance.main_v2.name
  password = random_password.db_user.result
}
