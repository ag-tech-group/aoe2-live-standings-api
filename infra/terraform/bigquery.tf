# BigQuery destination for the Cloud Billing usage-cost export.
#
# Why: the July 2026 budget investigation had to reconstruct spend bottom-up
# from resource metrics because nothing on this billing account exports
# per-SKU cost data (Cloud Billing has no read API for spend). This dataset
# is the destination; the export itself CANNOT be enabled by API or
# Terraform — it is a billing-account-level, console-only toggle:
#
#   Console → Billing (account 013A9E-059793-1B88C7) → Billing export →
#   BigQuery export → "Standard usage cost" → Edit settings → project
#   `aoe2-live-standings-api`, dataset `billing_export` → Save. Optionally
#   also enable "Detailed usage cost" (resource-level rows) into the same
#   dataset.
#
# Notes:
#   - The export covers the WHOLE billing account (every project billed to
#     it), not just this project — useful, but mind that when querying.
#   - Rows start flowing at enablement; there is no historical backfill.
#   - The console flow grants Google's export writer access automatically
#     when a Billing Account Administrator enables it.
resource "google_bigquery_dataset" "billing_export" {
  dataset_id  = "billing_export"
  description = "Cloud Billing usage-cost export destination (enabled console-side; covers the whole billing account). Standard/detailed usage cost tables land here."
  location    = "US"

  # Billing history is the point — never let a destroy take the data along.
  delete_contents_on_destroy = false
}
