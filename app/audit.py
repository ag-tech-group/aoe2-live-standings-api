"""Audit logging for the management (write) API.

The request log line in ``app.main:request_logging_middleware`` records
that *someone* did *something* (method + path + status); the audit
log records *who* did *what* — the actor's criticalbit user UUID, the
target resource (tournament slug, target user id, profile id, team
id), and the action enum.

Events are emitted from each write handler in ``app.routers.*`` after
its commit returns, so a rolled-back transaction never produces a
phantom audit entry. The log goes through a dedicated structlog
logger (``app.audit``) so a Cloud Logging sink can route the stream
into a long-lived audit bucket / BigQuery / similar.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import structlog

_logger = structlog.get_logger("app.audit")


class AuditAction(StrEnum):
    """Every state-changing action on the management API.

    Names are stable identifiers — downstream queries (Cloud Logging
    filters, BigQuery views, dashboards) can pin to these strings.
    Add new variants for new write endpoints; never rename or remove.
    """

    TOURNAMENT_CREATE = "tournament_create"
    TOURNAMENT_UPDATE = "tournament_update"
    TOURNAMENT_DELETE = "tournament_delete"

    OWNER_GRANT = "owner_grant"
    OWNER_REVOKE = "owner_revoke"

    ROSTER_ADD = "roster_add"
    ROSTER_REMOVE = "roster_remove"
    ROSTER_UPDATE = "roster_update"

    TEAM_CREATE = "team_create"
    TEAM_UPDATE = "team_update"
    TEAM_DELETE = "team_delete"

    TEAM_MEMBER_ADD = "team_member_add"
    TEAM_MEMBER_REMOVE = "team_member_remove"


def audit(
    action: AuditAction,
    *,
    actor_user_id: str,
    tournament_slug: str | None = None,
    tournament_id: int | None = None,
    target_user_id: str | None = None,
    target_profile_id: int | None = None,
    target_team_id: int | None = None,
    **extra: Any,
) -> None:
    """Emit one audit event.

    Only ``action`` and ``actor_user_id`` are required; the other
    fields populate the structured payload when meaningful for the
    action. ``extra`` allows action-specific fields (e.g. before /
    after diffs) without bloating this signature.

    The structlog PII processor scrubs sensitive field names before
    the event leaves the process, mirroring the chain on the main
    request logger.
    """
    payload: dict[str, Any] = {
        "actor_user_id": actor_user_id,
    }
    if tournament_slug is not None:
        payload["tournament_slug"] = tournament_slug
    if tournament_id is not None:
        payload["tournament_id"] = tournament_id
    if target_user_id is not None:
        payload["target_user_id"] = target_user_id
    if target_profile_id is not None:
        payload["target_profile_id"] = target_profile_id
    if target_team_id is not None:
        payload["target_team_id"] = target_team_id
    payload.update(extra)

    _logger.info(action.value, **payload)
