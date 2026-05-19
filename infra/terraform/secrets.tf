# DATABASE_URL secret — the only thing in Secret Manager for v1.
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
