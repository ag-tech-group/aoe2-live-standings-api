"""Cloud Function: forwards Cloud Monitoring alert notifications to Sentry.

Triggered by Pub/Sub messages from a Cloud Monitoring notification channel
of type ``pubsub``. The channel publishes the standard incident-notification
JSON (same payload shape as the webhook channel) to a topic; this function
subscribes, parses the payload, and calls ``sentry_sdk.capture_message`` so
the alert lands in the same Sentry project as the app's loud failures.

Why a forwarder at all: Cloud Monitoring has no native Sentry integration,
and we want one triage surface for infra alerts. Email flooded the
operator inbox at preview scale (see infra/terraform/monitoring.tf:26-42
for the postmortem), and Slack/PagerDuty is heavier than this project
needs. A 30-line Pub/Sub-triggered function is the minimum that gives us
Sentry routing without coupling alert plumbing to the worker (which would
defeat the purpose — if the worker is wedged, its own alert can't reach
Sentry through itself).

Only open-incident notifications fire a Sentry event. Closed-incident
notifications are ignored — surfacing both would double the Sentry noise
on every resolution. If we ever want resolution tracking in Sentry,
move to ``capture_event`` with a stable fingerprint and use the
``state=closed`` path to resolve the issue group instead.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import functions_framework
import sentry_sdk

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Sentry init at module scope so it runs once per cold start, not per
# invocation. Mirrors the no-DSN-is-a-no-op stance in app/sentry.py: a
# missing or empty DSN logs a warning and the function still acks the
# Pub/Sub message (otherwise the topic would back up indefinitely while
# someone seeds the secret).
_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _DSN:
    sentry_sdk.init(
        dsn=_DSN,
        environment=os.environ.get("ENVIRONMENT", "production"),
        # No tracing — this function is a pure forwarder, not part of a
        # request flow we want to profile.
        traces_sample_rate=0.0,
        # Tag every event from this function so it's filterable in Sentry
        # alongside the app's events.
        release=os.environ.get("FUNCTION_VERSION", "cloud-monitoring-to-sentry"),
    )
else:
    logger.warning("SENTRY_DSN not set — alerts will be logged but not forwarded.")


@functions_framework.cloud_event
def forward_alert(cloud_event: Any) -> None:
    """Entry point. ``cloud_event.data["message"]["data"]`` is base64-JSON.

    The Cloud Monitoring incident payload shape (version 1.2) we read:

    .. code-block:: json

        {
          "version": "1.2",
          "incident": {
            "policy_name": "...",
            "condition_name": "...",
            "summary": "Cloud SQL CPU > 70%",
            "state": "open" | "closed",
            "url": "https://console.cloud.google.com/...",
            "started_at": 1234567890,
            "resource": {...},
            "metric": {...}
          }
        }
    """
    message = (cloud_event.data or {}).get("message", {})
    payload_b64 = message.get("data", "")
    if not payload_b64:
        logger.warning("Pub/Sub message had no `data` field; nothing to forward.")
        return

    try:
        payload = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        # Malformed payload — log and ack. Retrying wouldn't help.
        logger.exception("Failed to decode Cloud Monitoring payload: %s", exc)
        return

    incident = payload.get("incident") or {}
    state = incident.get("state", "unknown")
    if state != "open":
        # Closed/acknowledged incidents would double-fire Sentry on every
        # resolution. Only surface the opening edge.
        logger.info("Ignoring incident with state=%s", state)
        return

    policy = incident.get("policy_name") or "<unknown policy>"
    summary = incident.get("summary") or policy
    url = incident.get("url")
    condition = incident.get("condition_name")

    if not _DSN:
        # No DSN — log the alert content (so it's not lost in the logs)
        # and return successfully so the Pub/Sub message is acked.
        logger.info("Alert (Sentry disabled): policy=%s summary=%s url=%s", policy, summary, url)
        return

    with sentry_sdk.push_scope() as scope:
        # Tags are searchable/filterable in Sentry's UI; extras are not
        # indexed but show up on the event detail page.
        scope.set_tag("source", "cloud-monitoring")
        scope.set_tag("policy", policy)
        if condition:
            scope.set_tag("condition", condition)
        if url:
            scope.set_extra("incident_url", url)
        scope.set_extra("raw_incident", incident)
        # All capacity alerts are warning-tier today. If we later add
        # severity-mapped policies (e.g. SQL CPU > 90% as ERROR), thread
        # a level through here based on policy_name or a documentation
        # field on the alert.
        sentry_sdk.capture_message(f"GCP alert: {summary}", level="warning")

    logger.info("Forwarded alert to Sentry: policy=%s summary=%s", policy, summary)
