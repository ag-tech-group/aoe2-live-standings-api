"""Player + roster endpoints under /v1/tournaments/{slug}/players.

Roster rows are one first-class entity (#187): every row has a ``name`` and
an optional ``profile_id`` link. List / detail / PATCH / DELETE all address
a row by its surrogate ``tournament_player_id``.
"""

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
        # Display name = roster `name` (set to the alias, mirroring the prod
        # Phase 1 backfill); the list sorts and renders by it.
        tournament = make_tournament("cup", profile_ids=[199325, 409748])
        for tp in tournament.tracked_players:
            tp.name = {199325: "VIT | Hera", 409748: "AB | TaToH"}[tp.profile_id]
        session.add(tournament)
        await session.commit()

        payload = (await client.get("/v1/tournaments/cup/players")).json()
        aliases = [p["alias"] for p in payload["items"]]
        assert aliases == ["AB | TaToH", "VIT | Hera"]
        assert [p["name"] for p in payload["items"]] == ["AB | TaToH", "VIT | Hera"]
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
        assert "Cookie" not in response.headers.get("Vary", "")

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
        assert "Cookie" not in response.headers.get("Vary", "")

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

    async def test_roster_exposes_tournament_player_id(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Every roster item — linked and unlinked — carries its
        # tournament_player_id (#167), the key the team-management endpoints
        # and PATCH/DELETE take. The FE sources it here to assign players
        # (including unlinked ones) to teams.
        session.add(make_player(1, alias="linked"))
        tournament = make_tournament("cup", profile_ids=[1])
        tournament.tracked_players.append(TournamentPlayer(name="unlinked"))
        session.add(tournament)
        await session.commit()

        expected_ids = {
            tp.id
            for tp in (
                await session.execute(
                    select(TournamentPlayer).where(TournamentPlayer.tournament_id == tournament.id)
                )
            ).scalars()
        }
        items = (await client.get("/v1/tournaments/cup/players")).json()["items"]
        assert all(isinstance(item["tournament_player_id"], int) for item in items)
        assert {item["tournament_player_id"] for item in items} == expected_ids
        # Unlinked row: profile_id null, but the addressing key is present.
        unlinked = next(item for item in items if item["profile_id"] is None)
        assert isinstance(unlinked["tournament_player_id"], int)


class TestGetPlayer:
    async def test_unknown_tournament_player_id_returns_404(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_player(1, alias="solo"))
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()
        assert (await client.get("/v1/tournaments/cup/players/99999")).status_code == 404

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
        tournament = make_tournament("cup", profile_ids=[1])
        tournament.tracked_players[0].name = "Hera"
        session.add(tournament)
        await session.commit()
        tpid = tournament.tracked_players[0].id

        payload = (await client.get(f"/v1/tournaments/cup/players/{tpid}")).json()
        assert payload["profile_id"] == 1
        assert payload["alias"] == "Hera"
        assert payload["name"] == "Hera"
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
        tournament = make_tournament("cup", profile_ids=[1])
        session.add(tournament)
        await session.commit()
        tpid = tournament.tracked_players[0].id

        payload = (
            await client.get(f"/v1/tournaments/cup/players/{tpid}", params={"match_limit": 2})
        ).json()
        assert len(payload["recent_matches"]) == 2

    async def test_match_limit_out_of_range_returns_422(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup", profile_ids=[1]))
        await session.commit()
        # Query validation fires before the lookup, so any id 422s here.
        response = await client.get("/v1/tournaments/cup/players/1", params={"match_limit": 999})
        assert response.status_code == 422

    async def test_detail_includes_presentation(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1, alias="Hera")
        session.add(player)
        tournament = make_tournament("cup", profile_ids=[1])
        tournament.tracked_players[0].presentation = {"bio": "AoE2"}
        session.add(tournament)
        await session.commit()
        tpid = tournament.tracked_players[0].id

        payload = (await client.get(f"/v1/tournaments/cup/players/{tpid}")).json()
        assert payload["presentation"] == {"bio": "AoE2"}

    async def test_detail_for_unlinked_returns_unified_shape(
        self, client: AsyncClient, session: AsyncSession
    ):
        # An unlinked entry is addressable by tournament_player_id (#187) and
        # returns the unified shape with empty polled enrichment — no 404.
        tournament = make_tournament("cup")
        unlinked = TournamentPlayer(name="iyouxin", presentation={"flag": "🇺🇦"})
        tournament.tracked_players.append(unlinked)
        session.add(tournament)
        await session.commit()
        tpid = unlinked.id

        response = await client.get(f"/v1/tournaments/cup/players/{tpid}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["profile_id"] is None
        assert payload["name"] == "iyouxin"
        assert payload["alias"] == "iyouxin"
        assert payload["ratings"] == []
        assert payload["recent_matches"] == []
        assert payload["last_polled_at"] is None
        assert payload["presentation"] == {"flag": "🇺🇦"}


class TestAddRosterPlayer:
    """POST /v1/tournaments/{slug}/players — owner-gated roster add."""

    async def test_owner_adds_linked_player(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post(
            "/v1/tournaments/cup/players",
            json={"name": "Hera", "profile_id": 199325},
        )
        assert response.status_code == 204

        row = (await session.execute(select(TournamentPlayer))).scalar_one()
        assert row.profile_id == 199325
        assert row.name == "Hera"

    async def test_added_player_is_visible_to_the_poller(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # The poller re-resolves the roster every cycle, so a player added
        # over HTTP is tracked without a redeploy.
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        await client.post(
            "/v1/tournaments/cup/players",
            json={"name": "newbie", "profile_id": 555},
        )
        assert 555 in await get_tracked_profile_ids(session)

    async def test_adding_a_duplicate_profile_id_is_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post(
            "/v1/tournaments/cup/players",
            json={"name": "dupe", "profile_id": 199325},
        )
        assert response.status_code == 409

    async def test_missing_name_is_422(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"profile_id": 199325})
        assert response.status_code == 422

    async def test_non_positive_profile_id_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post(
            "/v1/tournaments/cup/players",
            json={"name": "x", "profile_id": 0},
        )
        assert response.status_code == 422


class TestRemoveRosterPlayer:
    """DELETE /v1/tournaments/{slug}/players/{tournament_player_id} — owner-gated."""

    async def test_owner_removes_player(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID])
        session.add(tournament)
        await session.commit()
        tpid = tournament.tracked_players[0].id

        response = await client.delete(f"/v1/tournaments/cup/players/{tpid}")
        assert response.status_code == 204
        assert (await session.execute(select(TournamentPlayer))).scalars().all() == []

    async def test_removing_a_player_not_on_the_roster_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.delete("/v1/tournaments/cup/players/99999")
        assert response.status_code == 404


class TestUpdateRosterPlayer:
    """PATCH /v1/tournaments/{slug}/players/{tournament_player_id} — owner-gated."""

    async def test_owner_sets_presentation(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID])
        session.add(tournament)
        await session.commit()
        tpid = tournament.tracked_players[0].id

        response = await client.patch(
            f"/v1/tournaments/cup/players/{tpid}",
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
        tournament = make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID])
        session.add(tournament)
        await session.commit()
        tpid = tournament.tracked_players[0].id
        await client.patch(
            f"/v1/tournaments/cup/players/{tpid}",
            json={"presentation": {"bio": "old", "extra": 1}},
        )

        response = await client.patch(
            f"/v1/tournaments/cup/players/{tpid}",
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
        tournament = make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID])
        session.add(tournament)
        await session.commit()
        tpid = tournament.tracked_players[0].id

        response = await client.patch(
            f"/v1/tournaments/cup/players/{tpid}",
            json={"presentation": "not-an-object"},
        )
        assert response.status_code == 422

    async def test_oversize_presentation_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID])
        session.add(tournament)
        await session.commit()
        tpid = tournament.tracked_players[0].id

        response = await client.patch(
            f"/v1/tournaments/cup/players/{tpid}",
            json={"presentation": {"bio": "x" * 9000}},
        )
        assert response.status_code == 422

    async def test_player_not_on_roster_is_404(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup/players/99999",
            json={"presentation": {"bio": "hi"}},
        )
        assert response.status_code == 404

    async def test_non_owner_is_403(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as("11111111-1111-1111-1111-111111111111")
        tournament = make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID])
        session.add(tournament)
        await session.commit()
        tpid = tournament.tracked_players[0].id

        response = await client.patch(
            f"/v1/tournaments/cup/players/{tpid}",
            json={"presentation": {"bio": "hi"}},
        )
        assert response.status_code == 403


class TestUnlinkedRosterCRUD:
    """Unified roster lifecycle: add (with/without link), list, link, delete."""

    async def test_add_unlinked_with_name_only(
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

    async def test_add_with_both_profile_id_and_name_succeeds(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # The XOR is gone (#187): a linked entry with a display name is the
        # normal shape, not a 422.
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post(
            "/v1/tournaments/cup/players",
            json={"profile_id": 199325, "name": "Hera"},
        )
        assert response.status_code == 204
        row = (await session.execute(select(TournamentPlayer))).scalar_one()
        assert row.profile_id == 199325
        assert row.name == "Hera"

    async def test_add_without_name_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={})
        assert response.status_code == 422

    async def test_add_with_numeric_name_succeeds(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # #187 Phase 3 retired the numeric-name guard (addressing is by
        # surrogate id now), so a numeric display name is accepted.
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"name": "12345"})
        assert response.status_code == 204
        row = (await session.execute(select(TournamentPlayer))).scalar_one()
        assert row.name == "12345"
        assert row.profile_id is None

    async def test_add_duplicate_name_is_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        tournament.tracked_players.append(TournamentPlayer(name="iyouxin"))
        session.add(tournament)
        await session.commit()

        response = await client.post("/v1/tournaments/cup/players", json={"name": "iyouxin"})
        assert response.status_code == 409

    async def test_list_interleaves_unlinked_by_alpha(
        self, client: AsyncClient, session: AsyncSession
    ):
        # The unified list sorts by display name regardless of link state.
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
        assert alice["name"] == "Alice"
        assert alice["updated_at"] is None
        assert alice["ratings"] == []

    async def test_patch_links_profile_id_keeping_name(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # PATCH with profile_id LINKS an unlinked entry to a polled identity
        # (#187): additive — profile_id is set and the name is KEPT (the old
        # promotion cleared name; it no longer does).
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        unlinked = TournamentPlayer(name="iyouxin", presentation={"flag": "🇺🇦"})
        tournament.tracked_players.append(unlinked)
        session.add(tournament)
        await session.commit()
        tpid = unlinked.id

        response = await client.patch(
            f"/v1/tournaments/cup/players/{tpid}",
            json={"profile_id": 12345},
        )
        assert response.status_code == 204

        session.expire_all()
        row = (await session.execute(select(TournamentPlayer))).scalar_one()
        assert row.profile_id == 12345
        assert row.name == "iyouxin"  # kept, not cleared
        assert row.presentation == {"flag": "🇺🇦"}

    async def test_patch_link_carries_new_presentation_when_supplied(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        unlinked = TournamentPlayer(name="iyouxin", presentation={"flag": "🇺🇦"})
        tournament.tracked_players.append(unlinked)
        session.add(tournament)
        await session.commit()
        tpid = unlinked.id

        response = await client.patch(
            f"/v1/tournaments/cup/players/{tpid}",
            json={"profile_id": 12345, "presentation": {"bio": "fresh"}},
        )
        assert response.status_code == 204

        session.expire_all()
        row = (await session.execute(select(TournamentPlayer))).scalar_one()
        assert row.profile_id == 12345
        assert row.name == "iyouxin"
        assert row.presentation == {"bio": "fresh"}

    async def test_patch_already_linked_with_profile_id_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # profile_id is immutable once linked.
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID])
        session.add(tournament)
        await session.commit()
        tpid = tournament.tracked_players[0].id

        response = await client.patch(
            f"/v1/tournaments/cup/players/{tpid}",
            json={"profile_id": 12345},
        )
        assert response.status_code == 422

    async def test_link_to_existing_profile_id_is_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", profile_ids=[199325], owner_ids=[DEFAULT_TEST_USER_ID])
        unlinked = TournamentPlayer(name="iyouxin")
        tournament.tracked_players.append(unlinked)
        session.add(tournament)
        await session.commit()
        tpid = unlinked.id

        response = await client.patch(
            f"/v1/tournaments/cup/players/{tpid}",
            json={"profile_id": 199325},
        )
        assert response.status_code == 409

    async def test_delete_unlinked_by_id(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        tournament = make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID])
        unlinked = TournamentPlayer(name="iyouxin")
        tournament.tracked_players.append(unlinked)
        session.add(tournament)
        await session.commit()
        tpid = unlinked.id

        response = await client.delete(f"/v1/tournaments/cup/players/{tpid}")
        assert response.status_code == 204
        assert (await session.execute(select(TournamentPlayer))).scalars().all() == []

    async def test_unlinked_excluded_from_poller_tracked_ids(
        self, client: AsyncClient, session: AsyncSession
    ):
        tournament = make_tournament("cup", profile_ids=[199325])
        tournament.tracked_players.append(TournamentPlayer(name="iyouxin"))
        session.add(tournament)
        await session.commit()

        tracked = await get_tracked_profile_ids(session)
        assert tracked == [199325]
