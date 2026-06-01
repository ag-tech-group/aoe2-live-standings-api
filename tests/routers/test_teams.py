"""Team standings + team management under /v1/tournaments/{slug}/teams."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LiveMatchPlayer, MatchState, Team, TeamMember
from tests.conftest import (
    DEFAULT_TEST_USER_ID,
    make_match,
    make_player,
    make_player_rating,
    make_team,
    make_tournament,
)


class TestTeamStandings:
    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope/teams/standings")).status_code == 404

    async def test_no_teams_returns_empty_envelope(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/teams/standings")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_combined_rating_sum_and_average(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id, rating in ((1, 2000), (2, 2400)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=rating)
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team("Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["name"] == "Red"
        assert row["member_count"] == 2
        assert row["combined_rating_sum"] == 4400
        assert row["combined_rating_average"] == 2200.0
        # Members are sorted by rating desc.
        assert [m["profile_id"] for m in row["members"]] == [2, 1]

    async def test_sorted_by_combined_sum_descending(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id, rating in ((1, 1000), (2, 1000), (3, 2000), (4, 2000)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=rating)
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2, 3, 4], leaderboard_id=3)
        tournament.teams = [
            make_team("Weak", profile_ids=[1, 2]),
            make_team("Strong", profile_ids=[3, 4]),
        ]
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"]
        assert [t["name"] for t in items] == ["Strong", "Weak"]

    async def test_empty_team_appears_with_zero_aggregate(
        self, client: AsyncClient, session: AsyncSession
    ):
        tournament = make_tournament("cup", leaderboard_id=3)
        tournament.teams = [make_team("Ghosts")]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["member_count"] == 0
        assert row["combined_rating_sum"] == 0
        assert row["combined_rating_average"] == 0.0
        assert row["members"] == []

    async def test_member_without_leaderboard_rating_is_omitted(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Player 1 has a rating; player 2 has none on the tournament's leaderboard.
        rated = make_player(1)
        rated.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(rated)
        session.add(make_player(2))
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team("Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["member_count"] == 1
        assert [m["profile_id"] for m in row["members"]] == [1]

    async def test_cache_control_header_unauthenticated(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Viewer path: same split as the per-player standings (#96).
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/teams/standings")
        assert (
            response.headers["Cache-Control"] == "public, s-maxage=15, max-age=0, must-revalidate"
        )
        assert response.headers["Vary"] == "Cookie"

    async def test_cache_control_header_authenticated(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Admin path: cookie presence flips to `private, no-store` so
        # read-after-write skips cache layers (#105).
        session.add(make_tournament("cup"))
        await session.commit()
        client.cookies.set("criticalbit_access", "any-value")
        response = await client.get("/v1/tournaments/cup/teams/standings")
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.headers["Vary"] == "Cookie"


class TestTeamMemberIdentityAndLiveStatus:
    """``TeamMemberRead.country`` and ``in_match`` / ``live_match_id``.

    Mirrors ``TestStandingsInMatch`` in ``test_tournaments.py`` — the
    fields are sourced from the same row + helper as the per-player
    standings endpoint, so a member's live status here matches their
    standings row in the same poll cycle.
    """

    async def test_country_populates_on_team_members(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, country="kr")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        tournament.teams = [make_team("Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()

        member = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0][
            "members"
        ][0]
        assert member["country"] == "kr"

    async def test_in_match_true_with_live_match_id(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_match(601, state=MatchState.IN_PROGRESS, completed_at=None))
        session.add(LiveMatchPlayer(match_id=601, profile_id=1))
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        tournament.teams = [make_team("Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()

        member = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0][
            "members"
        ][0]
        assert member["in_match"] is True
        assert member["live_match_id"] == 601

    async def test_not_in_match_when_no_live_row(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        tournament.teams = [make_team("Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()

        member = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0][
            "members"
        ][0]
        assert member["in_match"] is False
        assert member["live_match_id"] is None

    async def test_in_match_is_per_member_within_a_team(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Two members on the same team — one in a live match, one not.
        # Live status must not bleed between members.
        for profile_id in (1, 2):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=2000)
            )
            session.add(player)
        session.add(make_match(602, state=MatchState.IN_PROGRESS, completed_at=None))
        session.add(LiveMatchPlayer(match_id=602, profile_id=1))
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team("Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        members = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0][
            "members"
        ]
        status = {m["profile_id"]: (m["in_match"], m["live_match_id"]) for m in members}
        assert status == {1: (True, 602), 2: (False, None)}


class TestCreateTeam:
    """POST /v1/tournaments/{slug}/teams — owner-gated team creation."""

    async def test_owner_creates_team(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post(
            "/v1/tournaments/cup/teams", json={"name": "Red", "initials": "RED"}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Red"
        assert body["initials"] == "RED"
        assert isinstance(body["id"], int)

    async def test_overlong_initials_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        # initials is capped at 8 characters.
        response = await client.post(
            "/v1/tournaments/cup/teams", json={"name": "Red", "initials": "TOOLONGXX"}
        )
        assert response.status_code == 422


class TestUpdateTeam:
    """PATCH /v1/tournaments/{slug}/teams/{team_id} — owner-gated."""

    async def test_owner_updates_team_name(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Red")]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.patch(f"/v1/tournaments/cup/teams/{team_id}", json={"name": "Blue"})
        assert response.status_code == 200
        assert response.json()["name"] == "Blue"

    async def test_unknown_team_is_404(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch("/v1/tournaments/cup/teams/999", json={"name": "X"})
        assert response.status_code == 404

    async def test_team_from_another_tournament_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # An owner of `cup` cannot reach a team that belongs to `other`.
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        other = make_tournament("other")
        other.teams = [make_team("Foreign")]
        session.add(other)
        await session.commit()
        foreign_team_id = other.teams[0].id

        response = await client.patch(
            f"/v1/tournaments/cup/teams/{foreign_team_id}", json={"name": "X"}
        )
        assert response.status_code == 404


class TestDeleteTeam:
    """DELETE /v1/tournaments/{slug}/teams/{team_id} — owner-gated."""

    async def test_owner_deletes_team_and_its_members(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.delete(f"/v1/tournaments/cup/teams/{team_id}")
        assert response.status_code == 204
        assert (await session.execute(select(Team))).scalars().all() == []
        # The team's membership rows cascade with it.
        assert (await session.execute(select(TeamMember))).scalars().all() == []


class TestAddTeamMember:
    """POST /v1/tournaments/{slug}/teams/{team_id}/members — owner-gated."""

    async def test_owner_adds_member(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Red")]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.post(
            f"/v1/tournaments/cup/teams/{team_id}/members", json={"profile_id": 199325}
        )
        assert response.status_code == 204
        members = (await session.execute(select(TeamMember.profile_id))).scalars().all()
        assert members == [199325]

    async def test_adding_a_duplicate_member_is_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Red", profile_ids=[199325])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.post(
            f"/v1/tournaments/cup/teams/{team_id}/members", json={"profile_id": 199325}
        )
        assert response.status_code == 409


class TestRemoveTeamMember:
    """DELETE /v1/tournaments/{slug}/teams/{team_id}/members/{profile_id}."""

    async def test_owner_removes_member(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Red", profile_ids=[199325])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.delete(f"/v1/tournaments/cup/teams/{team_id}/members/199325")
        assert response.status_code == 204
        assert (await session.execute(select(TeamMember))).scalars().all() == []

    async def test_removing_a_non_member_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Red")]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.delete(f"/v1/tournaments/cup/teams/{team_id}/members/199325")
        assert response.status_code == 404


class TestSetTeamCaptain:
    """PUT /v1/tournaments/{slug}/teams/{team_id}/captain — owner-gated."""

    async def test_owner_sets_captain(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.put(
            f"/v1/tournaments/cup/teams/{team_id}/captain", json={"profile_id": 2}
        )
        assert response.status_code == 204
        captain = (
            await session.execute(
                select(TeamMember.profile_id).where(
                    TeamMember.team_id == team_id, TeamMember.is_captain.is_(True)
                )
            )
        ).scalar_one_or_none()
        assert captain == 2

    async def test_setting_existing_captain_is_idempotent_noop(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        team = make_team("Red", profile_ids=[1, 2])
        team.members[0].is_captain = True  # profile 1 is already captain
        tournament.teams = [team]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.put(
            f"/v1/tournaments/cup/teams/{team_id}/captain", json={"profile_id": 1}
        )
        assert response.status_code == 204
        captains = (
            (
                await session.execute(
                    select(TeamMember.profile_id).where(
                        TeamMember.team_id == team_id, TeamMember.is_captain.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert captains == [1]

    async def test_setting_replaces_previous_captain(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        team = make_team("Red", profile_ids=[1, 2])
        team.members[0].is_captain = True  # profile 1 starts as captain
        tournament.teams = [team]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.put(
            f"/v1/tournaments/cup/teams/{team_id}/captain", json={"profile_id": 2}
        )
        assert response.status_code == 204
        captains = (
            (
                await session.execute(
                    select(TeamMember.profile_id).where(
                        TeamMember.team_id == team_id, TeamMember.is_captain.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert captains == [2]

    async def test_setting_non_member_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.put(
            f"/v1/tournaments/cup/teams/{team_id}/captain", json={"profile_id": 9999}
        )
        assert response.status_code == 404

    async def test_unauthenticated_is_401(self, client: AsyncClient, session: AsyncSession):
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.put(
            f"/v1/tournaments/cup/teams/{team_id}/captain", json={"profile_id": 1}
        )
        assert response.status_code == 401


class TestClearTeamCaptain:
    """DELETE /v1/tournaments/{slug}/teams/{team_id}/captain — owner-gated."""

    async def test_owner_clears_captain(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        team = make_team("Red", profile_ids=[1])
        team.members[0].is_captain = True
        tournament.teams = [team]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.delete(f"/v1/tournaments/cup/teams/{team_id}/captain")
        assert response.status_code == 204
        captain = (
            await session.execute(
                select(TeamMember.profile_id).where(
                    TeamMember.team_id == team_id, TeamMember.is_captain.is_(True)
                )
            )
        ).scalar_one_or_none()
        assert captain is None

    async def test_clearing_when_no_captain_is_204(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team("Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.delete(f"/v1/tournaments/cup/teams/{team_id}/captain")
        assert response.status_code == 204


class TestCaptainOnTeamStandings:
    """``is_captain`` surfaces on TeamMemberRead in the team-standings view."""

    async def test_is_captain_true_for_designated_member(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id in (1, 2):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=2000)
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        team = make_team("Red", profile_ids=[1, 2])
        team.members[1].is_captain = True  # profile 2 is captain
        tournament.teams = [team]
        session.add(tournament)
        await session.commit()

        members = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0][
            "members"
        ]
        captain_flags = {m["profile_id"]: m["is_captain"] for m in members}
        assert captain_flags == {1: False, 2: True}

    async def test_is_captain_false_when_team_has_no_captain(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        tournament.teams = [make_team("Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()

        member = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0][
            "members"
        ][0]
        assert member["is_captain"] is False
