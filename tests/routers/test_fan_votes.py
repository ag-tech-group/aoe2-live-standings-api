"""Community Hype voting endpoints (#210): PUT ballot / GET tallies / GET me.

The public-write contract the FE generates against. Voters are anonymous
(a ``voter_token`` convenience key, no auth); ballots replace atomically
and tallies aggregate on read.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_team, make_tournament


async def _seed(
    session: AsyncSession,
    slug: str = "hype",
    *,
    budget_players: int = 100,
    budget_teams: int = 100,
) -> tuple[list[int], list[int]]:
    """Create a tournament with a 3-player roster and two teams.

    Returns ``(player_target_ids, team_target_ids)`` — the stable surrogate
    ids (``tournament_player_id`` / ``team_id``) a ballot targets.
    """
    tournament = make_tournament(
        slug,
        profile_ids=[1, 2, 3],
        fan_vote_budget_players=budget_players,
        fan_vote_budget_teams=budget_teams,
    )
    session.add(tournament)
    tournament.teams.append(make_team(tournament, "Alpha", profile_ids=[1, 2]))
    tournament.teams.append(make_team(tournament, "Bravo", profile_ids=[3]))
    await session.commit()

    player_ids = sorted(tp.id for tp in tournament.tracked_players)
    team_ids = sorted(team.id for team in tournament.teams)
    return player_ids, team_ids


class TestSubmitBallot:
    """PUT /v1/tournaments/{slug}/fan-votes — replace the caller's ballot."""

    async def test_creates_ballot_and_echoes_persisted_state(
        self, client: AsyncClient, session: AsyncSession
    ):
        players, teams = await _seed(session)
        response = await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [
                    {"target_id": players[0], "coins": 60},
                    {"target_id": players[1], "coins": 40},
                ],
                "teams": [{"target_id": teams[0], "coins": 100}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert {(a["target_id"], a["coins"]) for a in body["players"]} == {
            (players[0], 60),
            (players[1], 40),
        }
        assert body["teams"] == [{"target_id": teams[0], "coins": 100}]

    async def test_persisted_and_visible_via_me(self, client: AsyncClient, session: AsyncSession):
        players, _ = await _seed(session)
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={"voter_token": "v1", "players": [{"target_id": players[0], "coins": 25}]},
        )
        me = (await client.get("/v1/tournaments/hype/fan-votes/me?voter_token=v1")).json()
        assert me["players"] == [{"target_id": players[0], "coins": 25}]
        assert me["teams"] == []

    async def test_replace_semantics_last_write_wins(
        self, client: AsyncClient, session: AsyncSession
    ):
        players, _ = await _seed(session)
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [
                    {"target_id": players[0], "coins": 50},
                    {"target_id": players[1], "coins": 50},
                ],
            },
        )
        # Reallocate entirely onto a third player.
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={"voter_token": "v1", "players": [{"target_id": players[2], "coins": 100}]},
        )
        me = (await client.get("/v1/tournaments/hype/fan-votes/me?voter_token=v1")).json()
        assert me["players"] == [{"target_id": players[2], "coins": 100}]

    async def test_idempotent_double_submit(self, client: AsyncClient, session: AsyncSession):
        players, _ = await _seed(session)
        payload = {"voter_token": "v1", "players": [{"target_id": players[0], "coins": 30}]}
        first = (await client.put("/v1/tournaments/hype/fan-votes", json=payload)).json()
        second = (await client.put("/v1/tournaments/hype/fan-votes", json=payload)).json()
        assert first == second
        # Tally shows the target once with coins 30, a single backer — no
        # duplicate rows from the resubmit.
        tallies = (await client.get("/v1/tournaments/hype/fan-votes/tallies")).json()
        assert tallies["players"] == [{"target_id": players[0], "coins": 30, "backers": 1}]

    async def test_empty_array_clears_only_that_category(
        self, client: AsyncClient, session: AsyncSession
    ):
        players, teams = await _seed(session)
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [{"target_id": players[0], "coins": 10}],
                "teams": [{"target_id": teams[0], "coins": 10}],
            },
        )
        # Clear players, leave teams unspecified-but-empty (replace clears it too).
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [],
                "teams": [{"target_id": teams[0], "coins": 10}],
            },
        )
        me = (await client.get("/v1/tournaments/hype/fan-votes/me?voter_token=v1")).json()
        assert me["players"] == []
        assert me["teams"] == [{"target_id": teams[0], "coins": 10}]

    async def test_zero_coin_entries_are_dropped(self, client: AsyncClient, session: AsyncSession):
        players, _ = await _seed(session)
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [
                    {"target_id": players[0], "coins": 100},
                    {"target_id": players[1], "coins": 0},
                ],
            },
        )
        me = (await client.get("/v1/tournaments/hype/fan-votes/me?voter_token=v1")).json()
        # The 0-coin target leaves no row — not a backer, not in the ballot.
        assert me["players"] == [{"target_id": players[0], "coins": 100}]

    async def test_budget_boundary_exact_is_allowed(
        self, client: AsyncClient, session: AsyncSession
    ):
        players, _ = await _seed(session, budget_players=100)
        response = await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [
                    {"target_id": players[0], "coins": 70},
                    {"target_id": players[1], "coins": 30},
                ],
            },
        )
        assert response.status_code == 200

    async def test_over_budget_players_returns_422(
        self, client: AsyncClient, session: AsyncSession
    ):
        players, _ = await _seed(session, budget_players=100)
        response = await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [
                    {"target_id": players[0], "coins": 60},
                    {"target_id": players[1], "coins": 60},
                ],
            },
        )
        assert response.status_code == 422

    async def test_over_budget_teams_returns_422(self, client: AsyncClient, session: AsyncSession):
        _, teams = await _seed(session, budget_teams=50)
        response = await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={"voter_token": "v1", "teams": [{"target_id": teams[0], "coins": 51}]},
        )
        assert response.status_code == 422

    async def test_unknown_player_target_returns_422(
        self, client: AsyncClient, session: AsyncSession
    ):
        players, _ = await _seed(session)
        bogus = max(players) + 999
        response = await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={"voter_token": "v1", "players": [{"target_id": bogus, "coins": 10}]},
        )
        assert response.status_code == 422

    async def test_unknown_team_target_returns_422(
        self, client: AsyncClient, session: AsyncSession
    ):
        _, teams = await _seed(session)
        bogus = max(teams) + 999
        response = await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={"voter_token": "v1", "teams": [{"target_id": bogus, "coins": 10}]},
        )
        assert response.status_code == 422

    async def test_duplicate_target_in_category_returns_422(
        self, client: AsyncClient, session: AsyncSession
    ):
        players, _ = await _seed(session)
        response = await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [
                    {"target_id": players[0], "coins": 10},
                    {"target_id": players[0], "coins": 20},
                ],
            },
        )
        assert response.status_code == 422

    async def test_negative_coins_returns_422(self, client: AsyncClient, session: AsyncSession):
        players, _ = await _seed(session)
        response = await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={"voter_token": "v1", "players": [{"target_id": players[0], "coins": -5}]},
        )
        assert response.status_code == 422

    async def test_missing_voter_token_returns_422(
        self, client: AsyncClient, session: AsyncSession
    ):
        await _seed(session)
        response = await client.put("/v1/tournaments/hype/fan-votes", json={"players": []})
        assert response.status_code == 422

    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        response = await client.put(
            "/v1/tournaments/nope/fan-votes", json={"voter_token": "v1", "players": []}
        )
        assert response.status_code == 404

    async def test_validation_is_atomic_across_categories(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Valid players + invalid teams → 422 and NOTHING is written
        # (validation runs before any delete/insert).
        players, _ = await _seed(session)
        response = await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [{"target_id": players[0], "coins": 50}],
                "teams": [{"target_id": 999999, "coins": 10}],
            },
        )
        assert response.status_code == 422
        me = (await client.get("/v1/tournaments/hype/fan-votes/me?voter_token=v1")).json()
        assert me["players"] == []
        assert me["teams"] == []

    async def test_turnstile_token_accepted_in_body(
        self, client: AsyncClient, session: AsyncSession
    ):
        # #211 verifies it; #210 accepts it as part of the contract.
        players, _ = await _seed(session)
        response = await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "turnstile_token": "0.cf-turnstile-response",
                "players": [{"target_id": players[0], "coins": 10}],
            },
        )
        assert response.status_code == 200


class TestTallies:
    """GET /v1/tournaments/{slug}/fan-votes/tallies — aggregate-on-read."""

    async def test_empty_when_no_votes(self, client: AsyncClient, session: AsyncSession):
        await _seed(session)
        body = (await client.get("/v1/tournaments/hype/fan-votes/tallies")).json()
        assert body == {"players": [], "teams": []}

    async def test_sums_coins_and_counts_distinct_backers(
        self, client: AsyncClient, session: AsyncSession
    ):
        players, _ = await _seed(session)
        # Two voters back player[0]; one also backs player[1].
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={"voter_token": "v1", "players": [{"target_id": players[0], "coins": 60}]},
        )
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v2",
                "players": [
                    {"target_id": players[0], "coins": 40},
                    {"target_id": players[1], "coins": 30},
                ],
            },
        )
        tallies = (await client.get("/v1/tournaments/hype/fan-votes/tallies")).json()
        by_target = {e["target_id"]: e for e in tallies["players"]}
        assert by_target[players[0]] == {"target_id": players[0], "coins": 100, "backers": 2}
        assert by_target[players[1]] == {"target_id": players[1], "coins": 30, "backers": 1}

    async def test_sorted_by_coins_descending(self, client: AsyncClient, session: AsyncSession):
        players, _ = await _seed(session)
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [
                    {"target_id": players[0], "coins": 10},
                    {"target_id": players[1], "coins": 80},
                    {"target_id": players[2], "coins": 10},
                ],
            },
        )
        tallies = (await client.get("/v1/tournaments/hype/fan-votes/tallies")).json()
        coins = [e["coins"] for e in tallies["players"]]
        assert coins == sorted(coins, reverse=True)
        assert tallies["players"][0]["target_id"] == players[1]

    async def test_both_categories_present(self, client: AsyncClient, session: AsyncSession):
        players, teams = await _seed(session)
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={
                "voter_token": "v1",
                "players": [{"target_id": players[0], "coins": 10}],
                "teams": [{"target_id": teams[0], "coins": 20}],
            },
        )
        tallies = (await client.get("/v1/tournaments/hype/fan-votes/tallies")).json()
        assert tallies["players"] == [{"target_id": players[0], "coins": 10, "backers": 1}]
        assert tallies["teams"] == [{"target_id": teams[0], "coins": 20, "backers": 1}]

    async def test_scoped_to_tournament(self, client: AsyncClient, session: AsyncSession):
        players_a, _ = await _seed(session, "cup-a")
        players_b, _ = await _seed(session, "cup-b")
        await client.put(
            "/v1/tournaments/cup-a/fan-votes",
            json={"voter_token": "v1", "players": [{"target_id": players_a[0], "coins": 50}]},
        )
        # cup-b has no votes despite cup-a having some.
        tallies_b = (await client.get("/v1/tournaments/cup-b/fan-votes/tallies")).json()
        assert tallies_b == {"players": [], "teams": []}

    async def test_cache_control_is_viewer_coalesced(
        self, client: AsyncClient, session: AsyncSession
    ):
        await _seed(session)
        response = await client.get("/v1/tournaments/hype/fan-votes/tallies")
        assert response.status_code == 200
        assert (
            response.headers["Cache-Control"] == "public, s-maxage=10, max-age=0, must-revalidate"
        )

    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope/fan-votes/tallies")).status_code == 404


class TestMyBallot:
    """GET /v1/tournaments/{slug}/fan-votes/me — the caller's ballot, for prefill."""

    async def test_unknown_voter_returns_empty(self, client: AsyncClient, session: AsyncSession):
        await _seed(session)
        body = (await client.get("/v1/tournaments/hype/fan-votes/me?voter_token=nobody")).json()
        assert body == {"players": [], "teams": []}

    async def test_isolated_per_voter(self, client: AsyncClient, session: AsyncSession):
        players, _ = await _seed(session)
        await client.put(
            "/v1/tournaments/hype/fan-votes",
            json={"voter_token": "v1", "players": [{"target_id": players[0], "coins": 10}]},
        )
        # A different token sees nothing of v1's ballot.
        other = (await client.get("/v1/tournaments/hype/fan-votes/me?voter_token=v2")).json()
        assert other == {"players": [], "teams": []}

    async def test_no_store_cache_header(self, client: AsyncClient, session: AsyncSession):
        await _seed(session)
        response = await client.get("/v1/tournaments/hype/fan-votes/me?voter_token=v1")
        assert response.headers["Cache-Control"] == "private, no-store"

    async def test_missing_voter_token_returns_422(
        self, client: AsyncClient, session: AsyncSession
    ):
        await _seed(session)
        assert (await client.get("/v1/tournaments/hype/fan-votes/me")).status_code == 422

    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        response = await client.get("/v1/tournaments/nope/fan-votes/me?voter_token=v1")
        assert response.status_code == 404
