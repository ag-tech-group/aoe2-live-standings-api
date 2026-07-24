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

locals {
  # DELETED for the post-event dormant period (2026-07-23, follow-up to the
  # PR #289 cost cleanup): a *stopped* instance of this shape still bills
  # ~$70/mo — dominated by the EP data cache, whose tier-fixed 375 GiB of
  # local SSD keeps billing while stopped ($0.000219178/GiB-h ≈ $60/mo),
  # plus the idle public IP ($0.01/h ≈ $7/mo), disk, and retained backups.
  # This WAS the July 2026 dormant bill: $50.77 for Jul 1-23,
  # "predominantly Cloud SQL" per billing support. The final state was
  # exported to
  # gs://aoe2-live-standings-api-db-archive/final/ (verified before deletion)
  # and the gcloud deletion retained a final backup as a second copy.
  #
  # Resurrection for the next event:
  #   1. Flip this to false and `tofu apply` — recreates instance + database +
  #      user with the same names and the same password (random_password.db_user
  #      persists in state, and the database-url / db-app-password secrets were
  #      never touched). NOTE: Cloud SQL blocks reusing a deleted instance's
  #      name for ~1 week after deletion; resurrect later than that (or bump
  #      the -vN suffix and the locals below).
  #   2. Import the archive dump (schema + data, incl. alembic_version, so
  #      deploys/migrations resume cleanly):
  #        gcloud sql import sql aoe2-standings-db-v2 \
  #          gs://aoe2-live-standings-api-db-archive/final/<dump>.sql.gz \
  #          --database=aoe2_live_standings
  #   3. Restore the rest of the stack per #285-#288 in reverse (worker min=1,
  #      ingress, REGIONAL if finals-grade) and re-enable the dormant-disabled
  #      alert policies + uptime checks (capacity_alerts.tf, monitoring.tf,
  #      uptime.tf).
  sql_instance_deleted = true

  # Literal identity of main_v2. Consumers (run.tf, jobs.tf, secrets.tf,
  # outputs.tf, dashboard.tf, capacity_alerts.tf) reference these locals
  # rather than resource attributes so the dormant deletion of the instance
  # resource doesn't cascade into them — every string is deterministic from
  # project/region/name. `database_id` metric labels are "project:instance"
  # (no region); the connection name is "project:region:instance".
  sql_instance_name_v2   = "${var.db_instance_name}-v2"
  sql_connection_name_v2 = "${var.project_id}:${var.region}:${local.sql_instance_name_v2}"
}

# Long-term archive for database exports — currently the final pre-deletion
# dump (the resurrection artifact). Regional + versioned-off + tiny (~one
# compressed dump): effectively $0/mo.
resource "google_storage_bucket" "db_archive" {
  name                        = "${var.project_id}-db-archive"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # The dump is the only low-friction path back to the event's data — don't
  # let a careless destroy eat it.
  lifecycle {
    prevent_destroy = true
  }
}

# Random app-user password, persisted to Terraform state (encrypted at rest in
# the GCS backend) and shared by main_v2's app user and both DB secrets
# (database-url, db-app-password). Rotating requires
# `tofu taint random_password.db_user` + apply + redeploy.
# Survives the dormant instance deletion on purpose: the recreated user gets
# the same password, so the untouched secrets stay correct.
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
  # Gated by the dormant deletion — see the sql_instance_deleted local above.
  count = local.sql_instance_deleted ? 0 : 1

  name             = local.sql_instance_name_v2
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

    # STOPPED for the dormant period: the FE now serves the frozen event fully
    # static (hera-streamer-invitational-2026-web#375), so nothing reads this
    # DB. "NEVER" halts compute (vCPU/RAM) billing but NOT everything — and
    # the remainder is dominated by the EP data cache below: its tier-fixed
    # 375 GiB of local SSD keeps billing while stopped ($0.000219178/GiB-h
    # ≈ $60/mo), plus the public IP at the idle rate ($0.01/h ≈ $7/mo),
    # disk (~$2/mo) and retained backups. A stopped instance of this shape
    # therefore floors at ~$70/mo (July 2026 actual: $50.77 for Jul 1-23,
    # "predominantly Cloud SQL" per billing support) — deleting it is the
    # only lower state. All data is preserved on disk while stopped.
    # Restart for the next event: set "ALWAYS".
    activation_policy = "NEVER"

    # Enterprise Plus data cache: extends the buffer pool onto local SSD
    # (tier-fixed 375 GiB on N-2). NOT "included with EP" as previously
    # claimed here — it bills separately ($0.000219178/GiB-h ≈ $60/mo) and
    # keeps billing while the instance is STOPPED; it was the bulk of the
    # July 2026 dormant bill. Managed Connection Pooling does not need it.
    # Default OFF: enable deliberately for finals-grade read latency, and
    # disable again as part of the post-event scale-down.
    data_cache_config {
      data_cache_enabled = false
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
  count = local.sql_instance_deleted ? 0 : 1

  name     = var.db_name
  instance = google_sql_database_instance.main_v2[0].name
}

# Reuse the same generated app password (random_password.db_user, formerly also
# the retired `main`'s user) so the DATABASE_URL secret format and the connector
# path share one credential.
resource "google_sql_user" "app_v2" {
  count = local.sql_instance_deleted ? 0 : 1

  name     = var.db_user
  instance = google_sql_database_instance.main_v2[0].name
  password = random_password.db_user.result
}
