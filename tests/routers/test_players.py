"""Player + roster endpoints under /v1/tournaments/{slug}/players."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TournamentPlayer
from app.poller.roster import get_tracked_profile_ids
from tests.conftest import (
    DEFAULT_TEST_USER_ID,
    make_match,
    make_match_player,
    make_player,
    make_player_rating,
    make_tournament,
)


class TestListPlayers:
    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope/players")).status_code == 404

    async def test_empty_roster_returns_empty_envelope(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/players")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_returns_roster_with_ratings_sorted_alphabetically(
        self, client: AsyncClient, session: AsyncSession
    ):
        hera = make_player(199325, alias="VIT | Hera")
        hera.ratings.append(make_player_rating(199325, leaderboard_id=3, current_rating=2788))
        tatoh = make_player(409748, alias="AB | TaToH")
        tatoh.ratings.append(make_player_rating(409748, leaderboard_id=3, current_rating=2454))
        session.add_all([hera, tatoh])
        session.add(make_tournament("cup", profile_ids=[199325, 409748]))
        await session.commit()

        payload = (await client.get("/v1/tournaments/cup/players")).json()
        aliases = [p["alias"] for p in payload["items"]]
        assert aliases == ["AB | TaToH", "VIT | Hera"]
        assert payload["last_polled_at"] is not None
        hera_payload = next(p for p in payload["items"] if p["profile_id"] == 199325)
        assert hera_payload["ratings"][0]["current_rating"] == 2788

    async def test_scoped_to_tournament_roster(self, client: AsyncClient, session: AsyncSession):
        # A player outside the roster is not listed.
        for profile_id in (1, 2):
            session.add(make_player(profile_id))
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/players")).json()["items"]
        assert [p["profile_id"] for p in items] == [1]

    async def test_leaderboard_id_filters_ratings_but_keeps_player(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, alias="solo")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=1800))
        player.ratings.append(make_player_rating(1, leaderboard_id=4, current_rating=1600))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()

        items = (
            await client.get("/v1/tournaments/cup/players", params={"leaderboard_id": 3})
        ).json()["items"]
        assert len(items) == 1
        assert len(items[0]["ratings"]) == 1
        assert items[0]["ratings"][0]["leaderboard_id"] == 3

    async def test_player_included_even_when_no_ratings_match_filter(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, alias="solo")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=1800))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()

        items = (
            await client.get("/v1/tournaments/cup/players", params={"leaderboard_id": 99})
        ).json()["items"]
        assert len(items) == 1
        assert items[0]["ratings"] == []

    async def test_cache_control_header_unauthenticated(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Viewer path: same split as standings (#96) — see test_tournaments.py.
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/players")
        assert (
            response.headers["Cache-Control"] == "public, s-maxage=15, max-age=0, must-revalidate"
        )
        assert response.headers["Vary"] == "Cookie"

    async def test_cache_control_header_authenticated(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Admin path: cookie presence flips to `private, no-store` so
        # roster mutations (#105) are reflected on the next read.
        session.add(make_tournament("cup"))
        await session.commit()
        client.cookies.set("criticalbit_access", "any-value")
        response = await client.get("/v1/tournaments/cup/players")
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.headers["Vary"] == "Cookie"

    async def test_list_includes_presentation(self, client: AsyncClient, session: AsyncSession):
        # The presentation bag is folded onto the roster list: set on one
        # player, empty {} on the other.
        session.add_all([make_player(1, alias="a"), make_player(2, alias="b")])
        tournament = make_tournament("cup", profile_ids=[1, 2])
        for tracked in tournament.tracked_players:
            if tracked.profile_id == 1:
                tracked.presentation = {"bio": "hi"}
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/players")).json()["items"]
        by_id = {p["profile_id"]: p for p in items}
        assert by_id[1]["presentation"] == {"bio": "hi"}
        assert by_id[2]["presentation"] == {}


class TestGetPlayer:
    async def test_profile_outside_roster_returns_404(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_player(2, alias="outsider"))
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()
        assert (await client.get("/v1/tournaments/cup/players/2")).status_code == 404

    async def test_returns_player_with_ratings_and_recent_matches(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, alias="Hera")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2788))
        session.add(player)
        for match_id in (10, 11, 12):
            match = make_match(match_id)
            match.players.append(make_match_player(match_id, profile_id=1))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()

        payload = (await client.get("/v1/tournaments/cup/players/1")).json()
        assert payload["alias"] == "Hera"
        assert payload["last_polled_at"] is not None
        assert len(payload["ratings"]) == 1
        assert len(payload["recent_matches"]) == 3
        assert all("players" in m for m in payload["recent_matches"])

    async def test_match_limit_caps_recent_matches(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_player(1, alias="Hera"))
        for i in range(5):
            match = make_match(100 + i)
            match.players.append(make_match_player(100 + i, profile_id=1))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()

        payload = (
            await client.get("/v1/tournaments/cup/players/1", params={"match_limit": 2})
        ).json()
        assert len(payload["recent_matches"]) == 2

    async def test_match_limit_out_of_range_returns_422(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/players/1", params={"match_limit": 999})
        assert response.status_code == 422

    async def test_detail_includes_presentation(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1, alias="Hera")
        session.add(player)
        tournament = make_tournament("cup", profile_ids=[1])
        tournament.tracked_players[0].presentation = {"bio": "AoE2"}
        session.add(tournament)
        await session.commit()

        payload = (await client.get("/v1/tournaments/cup/players/1")).json()
        assert payload["presentation"] == {"bio": "AoE2"}


class TestAddRosterPlayer:
    """POST /v1/tournaments/{slug}/players — owner-gated roster add."""

    async def test_owner_adds_player(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"profile_id": 199325})
        assert response.status_code == 204

        roster = (await session.execute(select(TournamentPlayer.profile_id))).scalars().all()
        assert roster == [199325]

    async def test_added_player_is_visible_to_the_poller(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # The poller re-resolves the roster every cycle, so a player added
        # over HTTP is tracked without a redeploy.
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        await client.post("/v1/tournaments/cup/players", json={"profile_id": 555})
        assert 555 in await get_tracked_profile_ids(session)

    async def test_adding_a_duplicate_is_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"profile_id": 199325})
        assert response.status_code == 409

    async def test_non_positive_profile_id_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"profile_id": 0})
        assert response.status_code == 422


class TestRemoveRosterPlayer:
    """DELETE /v1/tournaments/{slug}/players/{profile_id} — owner-gated."""

    async def test_owner_removes_player(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.delete("/v1/tournaments/cup/players/199325")
        assert response.status_code == 204
        assert (await session.execute(select(TournamentPlayer))).scalars().all() == []

    async def test_removing_a_player_not_on_the_roster_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.delete("/v1/tournaments/cup/players/199325")
        assert response.status_code == 404


class TestUpdateRosterPlayer:
    """PATCH /v1/tournaments/{slug}/players/{profile_id} — owner-gated presentation bag."""

    async def test_owner_sets_presentation(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/players/199325",
            json={"presentation": {"streamUrls": ["https://twitch.tv/hera"], "bio": "GOAT"}},
        )
        assert response.status_code == 204

        session.expire_all()
        entry = (
            await session.execute(
                select(TournamentPlayer).where(TournamentPlayer.profile_id == 199325)
            )
        ).scalar_one()
        assert entry.presentation == {"streamUrls": ["https://twitch.tv/hera"], "bio": "GOAT"}

    async def test_patch_replaces_whole_bag(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # The PATCH replaces the entire object — keys absent from the new
        # body are dropped, so an empty object clears the bag.
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()
        await client.patch(
            "/v1/tournaments/cup/players/199325",
            json={"presentation": {"bio": "old", "extra": 1}},
        )

        response = await client.patch(
            "/v1/tournaments/cup/players/199325",
            json={"presentation": {"bio": "new"}},
        )
        assert response.status_code == 204

        session.expire_all()
        entry = (
            await session.execute(
                select(TournamentPlayer).where(TournamentPlayer.profile_id == 199325)
            )
        ).scalar_one()
        assert entry.presentation == {"bio": "new"}

    async def test_non_object_presentation_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/players/199325",
            json={"presentation": "not-an-object"},
        )
        assert response.status_code == 422

    async def test_oversize_presentation_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/players/199325",
            json={"presentation": {"bio": "x" * 9000}},
        )
        assert response.status_code == 422

    async def test_profile_not_on_roster_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/players/199325",
            json={"presentation": {"bio": "hi"}},
        )
        assert response.status_code == 404

    async def test_non_owner_is_403(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as("11111111-1111-1111-1111-111111111111")
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/players/199325",
            json={"presentation": {"bio": "hi"}},
        )
        assert response.status_code == 403


class TestPlaceholderRosterCRUD:
    """Placeholder lifecycle in the unified roster: add / list / promote / delete."""

    async def test_add_placeholder_with_name(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post(
            "/v1/tournaments/cup/players",
            json={"name": "iyouxin", "presentation": {"flag": "🇺🇦"}},
        )
        assert response.status_code == 204

        row = (await session.execute(select(TournamentPlayer))).scalar_one()
        assert row.profile_id is None
        assert row.name == "iyouxin"
        assert row.presentation == {"flag": "🇺🇦"}

    async def test_add_with_both_profile_id_and_name_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post(
            "/v1/tournaments/cup/players",
            json={"profile_id": 199325, "name": "iyouxin"},
        )
        assert response.status_code == 422

    async def test_add_with_neither_profile_id_nor_name_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={})
        assert response.status_code == 422

    async def test_add_with_numeric_name_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # The polymorphic URL dispatch needs names to be non-numeric so
        # `/players/12345` is unambiguously a profile_id.
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"name": "12345"})
        assert response.status_code == 422

    async def test_add_duplicate_placeholder_name_is_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.tracked_players.append(TournamentPlayer(name="iyouxin"))
        session.add(tournament)
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"name": "iyouxin"})
        assert response.status_code == 409

    async def test_list_includes_placeholders_interleaved_by_alpha(
        self, client: AsyncClient, session: AsyncSession
    ):
        # The unified list sorts by display name regardless of row type.
        for profile_id, alias in ((1, "Marco"), (2, "Zeke")):
            session.add(make_player(profile_id, alias=alias))
        tournament = make_tournament("cup", profile_ids=[1, 2])
        tournament.tracked_players.append(TournamentPlayer(name="Alice"))
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/players")).json()["items"]
        assert [p["alias"] for p in items] == ["Alice", "Marco", "Zeke"]
        alice = next(p for p in items if p["alias"] == "Alice")
        assert alice["profile_id"] is None
        assert alice["updated_at"] is None
        assert alice["ratings"] == []

    async def test_patch_placeholder_promotes_when_profile_id_set(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # PATCH /players/iyouxin with profile_id atomically swaps the
        # row's identity from placeholder → polled and clears `name`.
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.tracked_players.append(
            TournamentPlayer(name="iyouxin", presentation={"flag": "🇺🇦"})
        )
        session.add(tournament)
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/players/iyouxin",
            json={"profile_id": 12345},
        )
        assert response.status_code == 204

        session.expire_all()
        row = (await session.execute(select(TournamentPlayer))).scalar_one()
        assert row.profile_id == 12345
        assert row.name is None
        # Presentation carries through unchanged.
        assert row.presentation == {"flag": "🇺🇦"}

    async def test_patch_promote_carries_new_presentation_when_supplied(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.tracked_players.append(
            TournamentPlayer(name="iyouxin", presentation={"flag": "🇺🇦"})
        )
        session.add(tournament)
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/players/iyouxin",
            json={"profile_id": 12345, "presentation": {"bio": "fresh"}},
        )
        assert response.status_code == 204

        session.expire_all()
        row = (await session.execute(select(TournamentPlayer))).scalar_one()
        assert row.profile_id == 12345
        assert row.presentation == {"bio": "fresh"}

    async def test_patch_real_player_with_profile_id_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # profile_id is immutable on a polled-identity row; promotion only
        # applies to placeholders.
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/players/199325",
            json={"profile_id": 12345},
        )
        assert response.status_code == 422

    async def test_promote_to_existing_profile_id_is_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.tracked_players.append(TournamentPlayer(name="iyouxin"))
        session.add(tournament)
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/players/iyouxin",
            json={"profile_id": 199325},
        )
        assert response.status_code == 409

    async def test_delete_placeholder_by_name(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.tracked_players.append(TournamentPlayer(name="iyouxin"))
        session.add(tournament)
        await session.commit()

        response = await client.delete("/v1/tournaments/cup/players/iyouxin")
        assert response.status_code == 204
        assert (await session.execute(select(TournamentPlayer))).scalars().all() == []

    async def test_get_player_detail_is_404_for_placeholder(
        self, client: AsyncClient, session: AsyncSession
    ):
        # The detail endpoint is profile_id-only; there's no detail to
        # show for a placeholder.
        tournament = make_tournament("cup")
        tournament.tracked_players.append(TournamentPlayer(name="iyouxin"))
        session.add(tournament)
        await session.commit()

        # Numeric paths look up by profile_id; a placeholder won't be found.
        response = await client.get("/v1/tournaments/cup/players/99999")
        assert response.status_code == 404

    async def test_placeholder_excluded_from_poller_tracked_ids(
        self, client: AsyncClient, session: AsyncSession
    ):
        tournament = make_tournament("cup", profile_ids=[199325])
        tournament.tracked_players.append(TournamentPlayer(name="iyouxin"))
        session.add(tournament)
        await session.commit()

        tracked = await get_tracked_profile_ids(session)
        assert tracked == [199325]
