"""Team standings + team management under /v1/tournaments/{slug}/teams."""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Civilization,
    LiveMatchPlayer,
    MatchOutcome,
    MatchState,
    Team,
    TeamMember,
    TournamentPlayer,
)
from tests.conftest import (
    DEFAULT_TEST_USER_ID,
    make_match,
    make_match_player,
    make_player,
    make_player_rating,
    make_player_rating_snapshot,
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
        for profile_id, peak in ((1, 2000), (2, 2400)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=peak, max_rating=peak
                )
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["name"] == "Red"
        assert row["member_count"] == 2
        # Sum and average are over members' peak (max_rating) on the
        # tournament's leaderboard.
        assert row["combined_rating_sum"] == 4400
        assert row["combined_rating_average"] == 2200.0
        # Members are sorted by peak desc.
        assert [m["profile_id"] for m in row["members"]] == [2, 1]

    async def test_combined_uses_peak_not_current(self, client: AsyncClient, session: AsyncSession):
        # Each member's current_rating sits well below their max_rating,
        # so a current-based sum would land at 3000 and a peak-based one
        # at 4400 — the assertion pins us to the peak path.
        for profile_id, current, peak in ((1, 1500, 2000), (2, 1500, 2400)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=current, max_rating=peak
                )
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["combined_rating_sum"] == 4400
        assert row["combined_rating_average"] == 2200.0

    async def test_members_sorted_by_peak_desc(self, client: AsyncClient, session: AsyncSession):
        # Profile 1 leads on current rating, profile 2 leads on peak —
        # peak determines the in-team order.
        for profile_id, current, peak in ((1, 2400, 1800), (2, 1500, 2400)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=current, max_rating=peak
                )
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        members = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0][
            "members"
        ]
        assert [m["profile_id"] for m in members] == [2, 1]

    async def test_current_rating_still_returned_on_members(
        self, client: AsyncClient, session: AsyncSession
    ):
        # ``current_rating`` is retained alongside ``max_rating`` so the
        # FE can keep its live/in-match overlays without a breaking
        # removal.
        player = make_player(1)
        player.ratings.append(
            make_player_rating(1, leaderboard_id=3, current_rating=1800, max_rating=2200)
        )
        session.add(player)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()

        member = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0][
            "members"
        ][0]
        assert member["current_rating"] == 1800
        assert member["max_rating"] == 2200

    async def test_sorted_by_combined_sum_descending(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id, peak in ((1, 1000), (2, 1000), (3, 2000), (4, 2000)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=peak, max_rating=peak
                )
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2, 3, 4], leaderboard_id=3)
        tournament.teams = [
            make_team(tournament, "Weak", profile_ids=[1, 2]),
            make_team(tournament, "Strong", profile_ids=[3, 4]),
        ]
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"]
        assert [t["name"] for t in items] == ["Strong", "Weak"]

    async def test_ranking_by_peak_overrides_current(
        self, client: AsyncClient, session: AsyncSession
    ):
        # ``current`` would put Cold first (3000 vs 2400); ``peak`` flips
        # that — Hot's roster has higher lifetime peaks (5000 vs 3600).
        # Ranking must follow peak.
        for profile_id, current, peak in (
            (1, 1500, 2500),
            (2, 1500, 2500),
            (3, 2500, 1800),
            (4, 2500, 1800),
        ):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=current, max_rating=peak
                )
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2, 3, 4], leaderboard_id=3)
        tournament.teams = [
            make_team(tournament, "Cold", profile_ids=[3, 4]),
            make_team(tournament, "Hot", profile_ids=[1, 2]),
        ]
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"]
        assert [t["name"] for t in items] == ["Hot", "Cold"]
        assert items[0]["combined_rating_sum"] == 5000
        assert items[1]["combined_rating_sum"] == 3600

    async def test_empty_team_appears_with_zero_aggregate(
        self, client: AsyncClient, session: AsyncSession
    ):
        tournament = make_tournament("cup", leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Ghosts")]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["member_count"] == 0
        assert row["combined_rating_sum"] == 0
        assert row["combined_rating_average"] == 0.0
        assert row["members"] == []

    async def test_member_without_leaderboard_rating_is_listed_with_null_ratings(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Player 1 has a rating on the tournament's leaderboard; player 2
        # has a polled identity but no rating row there yet (linked
        # account that hasn't played a ranked game). Both must surface
        # under ``members`` — player 2 with null rating fields — and the
        # unrated one is excluded from the combined aggregates (#166).
        rated = make_player(1)
        rated.ratings.append(
            make_player_rating(1, leaderboard_id=3, current_rating=2000, max_rating=2200)
        )
        session.add(rated)
        session.add(make_player(2, alias="newbie", country="us"))
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["member_count"] == 2
        # Unrated member is excluded from the peak-based aggregates.
        assert row["combined_rating_sum"] == 2200
        assert row["combined_rating_average"] == 2200.0
        # Both members surface; the unrated one is sorted to the tail
        # (max_rating null → NULLS LAST in the member sort).
        by_pid = {m["profile_id"]: m for m in row["members"]}
        assert [m["profile_id"] for m in row["members"]] == [1, 2]
        assert by_pid[2]["alias"] == "newbie"
        assert by_pid[2]["country"] == "us"
        assert by_pid[2]["current_rating"] is None
        assert by_pid[2]["max_rating"] is None

    async def test_member_without_polled_player_falls_back_to_roster_name(
        self, client: AsyncClient, session: AsyncSession
    ):
        # ``TeamMember`` with a profile_id whose ``Player`` row hasn't been
        # written by the poller yet — the standings left-joins ``Player`` so
        # the row surfaces with its roster ``name`` as the display alias
        # (#187: name is always present) and null country/ratings, rather
        # than vanishing.
        rated = make_player(1)
        rated.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(rated)
        tournament = make_tournament("cup", profile_ids=[1, 999], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 999])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["member_count"] == 2
        by_pid = {m["profile_id"]: m for m in row["members"]}
        assert by_pid[999]["alias"] == "p999"
        assert by_pid[999]["country"] is None
        assert by_pid[999]["current_rating"] is None
        assert by_pid[999]["max_rating"] is None

    async def test_unrated_only_team_returns_zero_aggregate_with_members_listed(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A team whose every member is linked-but-unrated still surfaces
        # its roster — combined sum/average stay 0 (no peaks to average)
        # and the FE can render the team. Guards the average's
        # zero-division path (#166).
        session.add(make_player(1, alias="a"))
        session.add(make_player(2, alias="b"))
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Newcomers", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["member_count"] == 2
        assert row["combined_rating_sum"] == 0
        assert row["combined_rating_average"] == 0.0
        assert {m["alias"] for m in row["members"]} == {"a", "b"}

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
        assert "Cookie" not in response.headers.get("Vary", "")

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
        assert "Cookie" not in response.headers.get("Vary", "")


class TestTeamStandingsFreezeAtWindowEnd:
    """Past ``grand_finals_date`` member peaks freeze at the as-of-window-end
    metric, so the combined sums, member order, and team order all hold even
    when the roster keeps laddering — the playoff seeding came from this
    table (mirrors ``/standings``)."""

    _BOUND = datetime(2026, 5, 20, 18, 0, 0, tzinfo=UTC)
    _IN_WINDOW = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC)
    _POST_WINDOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)

    async def test_post_window_ath_does_not_reorder_teams(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Red's player 1 peaked 1800 in-window then hit 2000 after the race;
        # Blue's player 2 held 1900 throughout. Live sums would say
        # Red (2000) > Blue (1900); the frozen table keeps Blue > Red.
        for profile_id, live_max in ((1, 2000), (2, 1900)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=1500, max_rating=live_max
                )
            )
            session.add(player)
        session.add(
            make_player_rating_snapshot(
                1, leaderboard_id=3, max_rating=1800, observed_at=self._IN_WINDOW
            )
        )
        session.add(
            make_player_rating_snapshot(
                1, leaderboard_id=3, max_rating=2000, observed_at=self._POST_WINDOW
            )
        )
        session.add(
            make_player_rating_snapshot(
                2, leaderboard_id=3, max_rating=1900, observed_at=self._IN_WINDOW
            )
        )
        tournament = make_tournament(
            "cup", profile_ids=[1, 2], leaderboard_id=3, grand_finals_date=self._BOUND
        )
        tournament.teams = [
            make_team(tournament, "Red", profile_ids=[1]),
            make_team(tournament, "Blue", profile_ids=[2]),
        ]
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"]
        assert [row["name"] for row in items] == ["Blue", "Red"]
        assert [row["combined_rating_sum"] for row in items] == [1900, 1800]
        # The surfaced member peak is the frozen metric too.
        assert items[1]["members"][0]["max_rating"] == 1800

    async def test_member_order_within_team_uses_frozen_peaks(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Within one team: player 1's live lifetime peak (2000) tops player
        # 2's (1900), but as of the bound it was 1800 — frozen order is 2, 1.
        for profile_id, live_max in ((1, 2000), (2, 1900)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=1500, max_rating=live_max
                )
            )
            session.add(player)
        session.add(
            make_player_rating_snapshot(
                1, leaderboard_id=3, max_rating=1800, observed_at=self._IN_WINDOW
            )
        )
        session.add(
            make_player_rating_snapshot(
                1, leaderboard_id=3, max_rating=2000, observed_at=self._POST_WINDOW
            )
        )
        session.add(
            make_player_rating_snapshot(
                2, leaderboard_id=3, max_rating=1900, observed_at=self._IN_WINDOW
            )
        )
        tournament = make_tournament(
            "cup", profile_ids=[1, 2], leaderboard_id=3, grand_finals_date=self._BOUND
        )
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert [m["profile_id"] for m in row["members"]] == [2, 1]
        assert [m["max_rating"] for m in row["members"]] == [1900, 1800]
        assert row["combined_rating_sum"] == 3700


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
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1])]
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
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1])]
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
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1])]
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
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
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
        tournament.teams = [make_team(tournament, "Red")]
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
        other.teams = [make_team(other, "Foreign")]
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
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[1, 2])
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
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

    async def test_owner_adds_polled_member(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[199325])
        tournament.teams = [make_team(tournament, "Red")]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id
        roster_id = tournament.tracked_players[0].id

        response = await client.post(
            f"/v1/tournaments/cup/teams/{team_id}/members",
            json={"tournament_player_id": roster_id},
        )
        assert response.status_code == 204
        members = (await session.execute(select(TeamMember.tournament_player_id))).scalars().all()
        assert members == [roster_id]

    async def test_owner_adds_placeholder_member(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # The original #167 ask: a placeholder roster row (announced
        # entrant whose profile_id hasn't minted yet) can now be teamed
        # via the surrogate roster id.
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.tracked_players.append(TournamentPlayer(name="Jabo"))
        tournament.teams = [make_team(tournament, "Red")]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id
        placeholder_roster_id = tournament.tracked_players[0].id

        response = await client.post(
            f"/v1/tournaments/cup/teams/{team_id}/members",
            json={"tournament_player_id": placeholder_roster_id},
        )
        assert response.status_code == 204
        members = (await session.execute(select(TeamMember.tournament_player_id))).scalars().all()
        assert members == [placeholder_roster_id]

    async def test_adding_a_duplicate_member_is_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[199325])
        tournament.teams = [make_team(tournament, "Red", profile_ids=[199325])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id
        roster_id = tournament.tracked_players[0].id

        response = await client.post(
            f"/v1/tournaments/cup/teams/{team_id}/members",
            json={"tournament_player_id": roster_id},
        )
        assert response.status_code == 409

    async def test_adding_a_roster_row_from_another_tournament_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # The surrogate roster id is global, but the URL path scopes
        # to one tournament — a roster id from another tournament is
        # unreachable from this URL and must 404 (not 500 or accept).
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.teams = [make_team(tournament, "Red")]
        other = make_tournament("other", profile_ids=[199325])
        session.add(tournament)
        session.add(other)
        await session.commit()
        team_id = tournament.teams[0].id
        foreign_roster_id = other.tracked_players[0].id

        response = await client.post(
            f"/v1/tournaments/cup/teams/{team_id}/members",
            json={"tournament_player_id": foreign_roster_id},
        )
        assert response.status_code == 404


class TestRemoveTeamMember:
    """DELETE /v1/tournaments/{slug}/teams/{team_id}/members/{tournament_player_id}."""

    async def test_owner_removes_member(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[199325])
        tournament.teams = [make_team(tournament, "Red", profile_ids=[199325])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id
        roster_id = tournament.tracked_players[0].id

        response = await client.delete(f"/v1/tournaments/cup/teams/{team_id}/members/{roster_id}")
        assert response.status_code == 204
        assert (await session.execute(select(TeamMember))).scalars().all() == []

    async def test_removing_a_non_member_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[199325])
        tournament.teams = [make_team(tournament, "Red")]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id
        roster_id = tournament.tracked_players[0].id

        response = await client.delete(f"/v1/tournaments/cup/teams/{team_id}/members/{roster_id}")
        assert response.status_code == 404


class TestSetTeamCaptain:
    """PATCH /v1/tournaments/{slug}/teams/{team_id}/captain — owner-gated."""

    async def test_owner_sets_captain(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[1, 2])
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id
        roster_ids_by_pid = {tp.profile_id: tp.id for tp in tournament.tracked_players}

        response = await client.patch(
            f"/v1/tournaments/cup/teams/{team_id}/captain",
            json={"tournament_player_id": roster_ids_by_pid[2]},
        )
        assert response.status_code == 204
        captain = (
            await session.execute(
                select(TeamMember.tournament_player_id).where(
                    TeamMember.team_id == team_id, TeamMember.is_captain.is_(True)
                )
            )
        ).scalar_one_or_none()
        assert captain == roster_ids_by_pid[2]

    async def test_setting_existing_captain_is_idempotent_noop(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[1, 2])
        team = make_team(tournament, "Red", profile_ids=[1, 2])
        team.members[0].is_captain = True  # profile 1 is already captain
        tournament.teams = [team]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id
        roster_ids_by_pid = {tp.profile_id: tp.id for tp in tournament.tracked_players}

        response = await client.patch(
            f"/v1/tournaments/cup/teams/{team_id}/captain",
            json={"tournament_player_id": roster_ids_by_pid[1]},
        )
        assert response.status_code == 204
        captains = (
            (
                await session.execute(
                    select(TeamMember.tournament_player_id).where(
                        TeamMember.team_id == team_id, TeamMember.is_captain.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert captains == [roster_ids_by_pid[1]]

    async def test_setting_replaces_previous_captain(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[1, 2])
        team = make_team(tournament, "Red", profile_ids=[1, 2])
        team.members[0].is_captain = True  # profile 1 starts as captain
        tournament.teams = [team]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id
        roster_ids_by_pid = {tp.profile_id: tp.id for tp in tournament.tracked_players}

        response = await client.patch(
            f"/v1/tournaments/cup/teams/{team_id}/captain",
            json={"tournament_player_id": roster_ids_by_pid[2]},
        )
        assert response.status_code == 204
        captains = (
            (
                await session.execute(
                    select(TeamMember.tournament_player_id).where(
                        TeamMember.team_id == team_id, TeamMember.is_captain.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert captains == [roster_ids_by_pid[2]]

    async def test_setting_non_member_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[1])
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.patch(
            f"/v1/tournaments/cup/teams/{team_id}/captain",
            json={"tournament_player_id": 9999},
        )
        assert response.status_code == 404

    async def test_unauthenticated_is_401(self, client: AsyncClient, session: AsyncSession):
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[1])
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id
        roster_id = tournament.tracked_players[0].id

        response = await client.patch(
            f"/v1/tournaments/cup/teams/{team_id}/captain",
            json={"tournament_player_id": roster_id},
        )
        assert response.status_code == 401


class TestClearTeamCaptain:
    """DELETE /v1/tournaments/{slug}/teams/{team_id}/captain — owner-gated."""

    async def test_owner_clears_captain(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[1])
        team = make_team(tournament, "Red", profile_ids=[1])
        team.members[0].is_captain = True
        tournament.teams = [team]
        session.add(tournament)
        await session.commit()
        team_id = tournament.teams[0].id

        response = await client.delete(f"/v1/tournaments/cup/teams/{team_id}/captain")
        assert response.status_code == 204
        captain = (
            await session.execute(
                select(TeamMember.tournament_player_id).where(
                    TeamMember.team_id == team_id, TeamMember.is_captain.is_(True)
                )
            )
        ).scalar_one_or_none()
        assert captain is None

    async def test_clearing_when_no_captain_is_204(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID], profile_ids=[1])
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1])]
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
        team = make_team(tournament, "Red", profile_ids=[1, 2])
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
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()

        member = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0][
            "members"
        ][0]
        assert member["is_captain"] is False


class TestTeamStandingsAggregates:
    """combined_wins/losses + win_pct + per-team civ aggregation on team standings (#220)."""

    async def test_combined_and_member_win_loss(self, client: AsyncClient, session: AsyncSession):
        for profile_id in (1, 2):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=2000, max_rating=2000
                )
            )
            session.add(player)
        # P1: 2 wins, 1 loss; P2: 1 win, 1 loss (all completed, leaderboard 3).
        for match_id, profile_id, outcome in (
            (1, 1, MatchOutcome.WIN),
            (2, 1, MatchOutcome.WIN),
            (3, 1, MatchOutcome.LOSS),
            (4, 2, MatchOutcome.WIN),
            (5, 2, MatchOutcome.LOSS),
        ):
            match = make_match(match_id, leaderboard_id=3)
            match.players.append(
                make_match_player(match_id, profile_id=profile_id, outcome=outcome)
            )
            session.add(match)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        # Combined = sum of members': 3 wins, 2 losses → 60.0%.
        assert row["combined_wins"] == 3
        assert row["combined_losses"] == 2
        assert row["win_pct"] == 60.0
        members = {m["profile_id"]: m for m in row["members"]}
        assert (members[1]["wins"], members[1]["losses"]) == (2, 1)
        assert (members[2]["wins"], members[2]["losses"]) == (1, 1)

    async def test_team_civ_aggregation(self, client: AsyncClient, session: AsyncSession):
        for profile_id in (1, 2):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=2000, max_rating=2000
                )
            )
            session.add(player)
        # P1: civ27 (win, loss) + civ13 (win); P2: civ27 (win).
        for match_id, profile_id, civ, outcome in (
            (1, 1, 27, MatchOutcome.WIN),
            (2, 1, 27, MatchOutcome.LOSS),
            (3, 1, 13, MatchOutcome.WIN),
            (4, 2, 27, MatchOutcome.WIN),
        ):
            match = make_match(match_id, leaderboard_id=3)
            match.players.append(
                make_match_player(
                    match_id, profile_id=profile_id, civilization_id=civ, outcome=outcome
                )
            )
            session.add(match)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        # Members' civs merged: civ27 picks3 wins2, civ13 picks1 win1. Picks desc.
        assert row["civs"] == [
            {"civilization_id": 27, "name": None, "picks": 3, "wins": 2},
            {"civilization_id": 13, "name": None, "picks": 1, "wins": 1},
        ]

    async def test_no_games_means_null_win_pct_and_empty_civs(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id in (1, 2):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=2000, max_rating=2000
                )
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1, 2])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["combined_wins"] == 0
        assert row["combined_losses"] == 0
        assert row["win_pct"] is None
        assert row["civs"] == []
        assert all((m["wins"], m["losses"]) == (0, 0) for m in row["members"])

    async def test_aggregates_windowed_to_tournament_dates(
        self, client: AsyncClient, session: AsyncSession
    ):
        # The team endpoint passes the tournament window through to both the
        # W/L and civ helpers — a pre-window match must not count.
        player = make_player(1)
        player.ratings.append(
            make_player_rating(1, leaderboard_id=3, current_rating=2000, max_rating=2000)
        )
        session.add(player)
        for match_id, day, outcome in ((1, 1, MatchOutcome.WIN), (2, 6, MatchOutcome.WIN)):
            match = make_match(
                match_id, leaderboard_id=3, started_at=datetime(2026, 6, day, 12, 0, tzinfo=UTC)
            )
            match.players.append(
                make_match_player(match_id, profile_id=1, civilization_id=27, outcome=outcome)
            )
            session.add(match)
        tournament = make_tournament(
            "cup",
            profile_ids=[1],
            leaderboard_id=3,
            start_date=datetime(2026, 6, 5, tzinfo=UTC),
            grand_finals_date=datetime(2026, 6, 30, tzinfo=UTC),
        )
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        # Only the 06-06 win is in-window; the 06-01 win is excluded.
        assert row["combined_wins"] == 1
        assert row["civs"] == [{"civilization_id": 27, "name": None, "picks": 1, "wins": 1}]

    async def test_team_civs_fold_in_civilization_name(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Team civ entries carry the civ name from the reference (#227).
        session.add(Civilization(civilization_id=27, name="Magyars"))
        player = make_player(1)
        player.ratings.append(
            make_player_rating(1, leaderboard_id=3, current_rating=2000, max_rating=2000)
        )
        session.add(player)
        match = make_match(1, leaderboard_id=3)
        match.players.append(
            make_match_player(1, profile_id=1, civilization_id=27, outcome=MatchOutcome.WIN)
        )
        session.add(match)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Red", profile_ids=[1])]
        session.add(tournament)
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/teams/standings")).json()["items"][0]
        assert row["civs"] == [{"civilization_id": 27, "name": "Magyars", "picks": 1, "wins": 1}]
