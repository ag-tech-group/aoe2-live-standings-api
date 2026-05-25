"""Audit-log emission tests (#55).

The audit emitter is mocked per-test via the `audit_events` fixture in
``tests/conftest.py``; each test asserts the right action enum + the
right actor/target identifiers reached the emitter. Verifies that
write handlers across all four routers (tournaments, owners, players,
teams) emit consistent events without relying on log capture.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from tests.conftest import (
    DEFAULT_TEST_USER_ID,
    make_team,
    make_tournament,
)

OTHER_USER_ID = "00000000-0000-0000-0000-0000000000bb"


class TestTournamentAudits:
    async def test_create_emits_tournament_create(
        self, client: AsyncClient, session: AsyncSession, auth_as, audit_events
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post(
            "/v1/tournaments",
            json={"slug": "spring-cup", "name": "Spring Cup", "leaderboard_id": 3},
        )
        assert response.status_code == 201
        assert len(audit_events) == 1
        e = audit_events[0]
        assert e["action"] == AuditAction.TOURNAMENT_CREATE
        assert e["actor_user_id"] == DEFAULT_TEST_USER_ID
        assert e["tournament_slug"] == "spring-cup"

    async def test_update_emits_tournament_update_with_changes(
        self, client: AsyncClient, session: AsyncSession, auth_as, audit_events
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        auth_as(DEFAULT_TEST_USER_ID)

        await client.patch("/v1/tournaments/cup", json={"name": "Renamed"})
        e = audit_events[0]
        assert e["action"] == AuditAction.TOURNAMENT_UPDATE
        assert e["changes"] == {"name": "Renamed"}

    async def test_delete_emits_tournament_delete(
        self, client: AsyncClient, session: AsyncSession, auth_as, audit_events
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        auth_as(DEFAULT_TEST_USER_ID)

        await client.delete("/v1/tournaments/cup")
        e = audit_events[0]
        assert e["action"] == AuditAction.TOURNAMENT_DELETE
        assert e["tournament_slug"] == "cup"

    async def test_failed_update_emits_no_audit(
        self, client: AsyncClient, session: AsyncSession, auth_as, audit_events
    ):
        # A request that 422s on validation must not emit a phantom
        # audit event — the rollback semantics #55 calls out.
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        auth_as(DEFAULT_TEST_USER_ID)

        response = await client.patch(
            "/v1/tournaments/cup",
            json={
                "start_date": "2026-07-01T00:00:00Z",
                "grand_finals_date": "2026-06-01T00:00:00Z",
            },
        )
        assert response.status_code == 422
        assert audit_events == []


class TestOwnerAudits:
    async def test_grant_emits_owner_grant_with_target_user_id(
        self, client: AsyncClient, session: AsyncSession, auth_as, audit_events
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        auth_as(DEFAULT_TEST_USER_ID)

        await client.post("/v1/tournaments/cup/owners", json={"user_id": OTHER_USER_ID})
        e = audit_events[0]
        assert e["action"] == AuditAction.OWNER_GRANT
        assert e["actor_user_id"] == DEFAULT_TEST_USER_ID
        assert e["target_user_id"] == OTHER_USER_ID

    async def test_revoke_emits_owner_revoke(
        self, client: AsyncClient, session: AsyncSession, auth_as, audit_events
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID, OTHER_USER_ID]))
        await session.commit()
        auth_as(DEFAULT_TEST_USER_ID)

        await client.delete(f"/v1/tournaments/cup/owners/{OTHER_USER_ID}")
        e = audit_events[0]
        assert e["action"] == AuditAction.OWNER_REVOKE
        assert e["target_user_id"] == OTHER_USER_ID


class TestRosterAudits:
    async def test_add_emits_roster_add(
        self, client: AsyncClient, session: AsyncSession, auth_as, audit_events
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        auth_as(DEFAULT_TEST_USER_ID)

        await client.post("/v1/tournaments/cup/players", json={"profile_id": 12345})
        e = audit_events[0]
        assert e["action"] == AuditAction.ROSTER_ADD
        assert e["target_profile_id"] == 12345


class TestTeamAudits:
    async def test_create_team_emits_team_create(
        self, client: AsyncClient, session: AsyncSession, auth_as, audit_events
    ):
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        auth_as(DEFAULT_TEST_USER_ID)

        await client.post(
            "/v1/tournaments/cup/teams",
            json={"name": "Reds", "initials": "RED"},
        )
        e = audit_events[0]
        assert e["action"] == AuditAction.TEAM_CREATE
        assert e["name"] == "Reds"

    async def test_add_member_emits_team_member_add(
        self, client: AsyncClient, session: AsyncSession, auth_as, audit_events
    ):
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Reds")]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id
        auth_as(DEFAULT_TEST_USER_ID)

        await client.post(
            f"/v1/tournaments/cup/teams/{team_id}/members",
            json={"profile_id": 99},
        )
        e = audit_events[0]
        assert e["action"] == AuditAction.TEAM_MEMBER_ADD
        assert e["target_profile_id"] == 99
        assert e["target_team_id"] == team_id
