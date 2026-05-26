# Secret Manager references.
#
# Two secrets live here, with deliberately different management models
# reflecting where their values come from:
#
#   - DATABASE_URL — fully TF-managed. The value is computable from
#     other TF resources (the generated DB user password + the SQL
#     instance's connection name), so the version's `secret_data` is
#     just a `format()` over those resources.
#
#   - SENTRY_DSN — externally managed. The value is issued by Sentry
#     and can't be computed from anything TF owns, so we don't try.
#     The secret container and its versions are created out-of-band by
#     an operator (see the data block's comment for the seeding recipe);
#     TF just references the existing secret via a `data` block. This
#     intentionally avoids the previous footgun where an empty
#     `var.sentry_dsn` would silently remove the env var from prod on
#     a careless `tofu apply` (resolved by #91).

# --- DATABASE_URL ----------------------------------------------------------
#
# URL targets the Cloud SQL Auth Proxy's Unix socket (mounted by Cloud Run
# at /cloudsql/<connection_name>). asyncpg accepts the directory path as
# `host=` and appends /.s.PGSQL.5432 itself.

resource "google_secret_manager_secret" "database_url" {
  secret_id = "database-url"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "database_url" {
  secret = google_secret_manager_secret.database_url.id
  secret_data = format(
    "postgresql+asyncpg://%s:%s@/%s?host=/cloudsql/%s",
    google_sql_user.app.name,
    random_password.db_user.result,
    google_sql_database.app.name,
    google_sql_database_instance.main.connection_name,
  )
}

# --- SENTRY_DSN ------------------------------------------------------------
#
# The Cloud Run services in `run.tf` reference this via `secret_key_ref`
# with `version = "latest"`. The runtime service account has project-level
# `roles/secretmanager.secretAccessor` (see iam.tf), so no per-secret IAM
# is needed — any secret in the project is automatically readable by the
# services.
#
# Pre-apply requirement: this secret + at least one version must exist
# in the project before `tofu apply` succeeds (the `data` lookup happens
# at plan time, and Cloud Run validates `secret_key_ref` at deploy time).
# One-time seeding recipe:
#
#   gcloud --account=<email> --project=aoe2-live-standings-api \
#     secrets create sentry-dsn --replication-policy=automatic
#
#   echo -n "<DSN value>" \
#     | gcloud --account=<email> --project=aoe2-live-standings-api \
#         secrets versions add sentry-dsn --data-file=-
#
# Rotation: add a new version (`gcloud secrets versions add sentry-dsn …`);
# Cloud Run picks up `version = "latest"` on the next revision roll. No TF
# action needed.

data "google_secret_manager_secret" "sentry_dsn" {
  secret_id = "sentry-dsn"
}
