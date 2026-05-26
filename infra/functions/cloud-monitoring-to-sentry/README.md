# cloud-monitoring-to-sentry

Cloud Function (2nd gen, Python 3.12, Pub/Sub-triggered) that forwards Cloud Monitoring incident notifications to Sentry.

## Why

Cloud Monitoring has no native Sentry destination. We want one triage surface for infra alerts (per `feedback_infra_alert_routing` in the operator's memory and the postmortem at `infra/terraform/monitoring.tf:26–42`). Email flooded the inbox at preview scale; Slack/PagerDuty is overkill. A small forwarder is the lightest path that gets GCP-native alerts (Cloud SQL CPU, Cloud Run connection counts, etc.) into Sentry alongside the app's exceptions.

## Wiring

```
Cloud Monitoring alert policy
   │
   ▼
google_monitoring_notification_channel (type=pubsub)
   │
   ▼
Pub/Sub topic: cloud-monitoring-alerts
   │
   ▼
This Cloud Function (forward_alert)
   │
   ▼
sentry_sdk.capture_message  →  Sentry project
```

Terraform for the topic, channel, function, and IAM lives in `infra/terraform/alerts_sentry.tf`. The alert policies that *use* the channel live in `infra/terraform/capacity_alerts.tf`.

## Behavior

- **`state=open` only.** Closed incidents are ignored — surfacing them would double-fire Sentry on resolution. If we later want resolution tracking, switch to `capture_event` with a stable fingerprint.
- **Tags**: `source=cloud-monitoring`, `policy=<policy_name>`, `condition=<condition_name>`. Searchable in Sentry.
- **Extras**: `incident_url` (link back to the Cloud Monitoring UI), `raw_incident` (full payload for debugging).
- **Level**: all alerts at `warning` today. Severity mapping can be added later if we have ERROR-tier policies.
- **No-DSN handling**: if `SENTRY_DSN` is unset, the function still acks the Pub/Sub message and logs the alert content. Otherwise the topic backs up indefinitely waiting on someone to seed the secret.

## Testing

No CI test runs against this directory (project `pytest` is scoped to `tests/`). Verify post-deploy by publishing a synthetic incident to the topic:

```bash
# Adapt project/account to your env; see CLAUDE.md gcloud rules.
gcloud --account=<email> --project=aoe2-live-standings-api \
  pubsub topics publish cloud-monitoring-alerts \
  --message='{"version":"1.2","incident":{"policy_name":"smoke-test","state":"open","summary":"Test from CLI","url":"https://example.com"}}'
```

Then check the Sentry project for a `GCP alert: Test from CLI` event tagged `source=cloud-monitoring`.

## Local development

```bash
cd infra/functions/cloud-monitoring-to-sentry
uv venv && uv pip install -r requirements.txt functions-framework
SENTRY_DSN=<dsn> functions-framework --target=forward_alert --signature-type=cloudevent --port=8081
# In another shell, POST a synthetic CloudEvent to localhost:8081.
```
