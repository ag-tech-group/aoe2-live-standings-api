"""GET /v1/tournaments, /v1/tournaments/{slug}, and /{slug}/standings."""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    HostLiveStream,
    LiveMatchPlayer,
    LiveStream,
    MatchOutcome,
    MatchState,
    Team,
    TeamMember,
    Tournament,
    TournamentOwner,
    TournamentPlayer,
)
from app.routers.tournaments import _RECENT_RESULTS_LIMIT
from tests.conftest import (
    DEFAULT_TEST_USER_ID,
    make_match,
    make_match_player,
    make_player,
    make_player_rating,
    make_team,
    make_tournament,
)


class TestListTournaments:
    async def test_empty_returns_empty_list(self, client: AsyncClient):
        response = await client.get("/v1/tournaments")
        assert response.status_code == 200
        assert response.json() == []

    async def test_lists_tournaments_newest_first(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("older", created_at=datetime(2026, 1, 1, tzinfo=UTC)))
        session.add(make_tournament("newer", created_at=datetime(2026, 2, 1, tzinfo=UTC)))
        await session.commit()

        items = (await client.get("/v1/tournaments")).json()
        assert [t["slug"] for t in items] == ["newer", "older"]

    async def test_cache_control_header(self, client: AsyncClient, session: AsyncSession):
        # Config split-cache (same as tournament detail): CF coalesces for
        # viewers, browser always revalidates. Must be explicit since the
        # #103 middleware default is `no-store`.
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments")
        assert response.status_code == 200
        assert (
            response.headers["Cache-Control"] == "public, s-maxage=15, max-age=0, must-revalidate"
        )

    async def test_host_stream_live_set_per_tournament(
        self, client: AsyncClient, session: AsyncSession
    ):
        """#149: each listed tournament's host_stream_live reflects only its own host."""
        live = make_tournament("live-cup", host_stream_urls=["https://twitch.tv/host"])
        quiet = make_tournament("quiet-cup")
        session.add(live)
        session.add(quiet)
        await session.flush()
        session.add(HostLiveStream(tournament_id=live.id, platform="twitch"))
        await session.commit()

        items = (await client.get("/v1/tournaments")).json()
        by_slug = {t["slug"]: t for t in items}
        assert by_slug["live-cup"]["host_stream_live"] is True
        assert by_slug["quiet-cup"]["host_stream_live"] is False


class TestGetTournamentDetail:
    async def test_returns_metadata(self, client: AsyncClient, session: AsyncSession):
        session.add(
            make_tournament(
                "cup",
                name="The Cup",
                leaderboard_id=3,
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        body = (await client.get("/v1/tournaments/cup")).json()
        assert body["slug"] == "cup"
        assert body["name"] == "The Cup"
        assert body["leaderboard_id"] == 3
        assert body["start_date"].startswith("2026-06-01")

    async def test_grand_finals_date_round_trips(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", grand_finals_date=datetime(2026, 6, 15, 18, tzinfo=UTC)))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup")).json()
        assert body["grand_finals_date"].startswith("2026-06-15T18")

    async def test_prize_pool_cents_round_trips(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", prize_pool_cents=512750))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup")).json()
        assert body["prize_pool_cents"] == 512750

    async def test_host_stream_urls_default_empty(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup"))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup")).json()
        assert body["host_stream_urls"] == []
        # No URLs configured → never live.
        assert body["host_stream_live"] is False

    async def test_host_stream_live_reflects_host_live_streams_table(
        self, client: AsyncClient, session: AsyncSession
    ):
        """#149: host_stream_live = any row for this tournament in host_live_streams."""
        tournament = make_tournament(
            "cup",
            host_stream_urls=["https://twitch.tv/hostchan"],
        )
        session.add(tournament)
        await session.flush()
        session.add(HostLiveStream(tournament_id=tournament.id, platform="twitch"))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup")).json()
        assert body["host_stream_urls"] == ["https://twitch.tv/hostchan"]
        assert body["host_stream_live"] is True

    async def test_unknown_slug_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope")).status_code == 404

    async def test_cache_control_header(self, client: AsyncClient, session: AsyncSession):
        # Same split-cache posture as the live endpoints (#96 / #104).
        # Tournament metadata is admin-mutated via PATCH, so the browser
        # must always revalidate even though the data changes rarely.
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup")
        assert response.status_code == 200
        assert (
            response.headers["Cache-Control"] == "public, s-maxage=15, max-age=0, must-revalidate"
        )


class TestCurrentSlugAlias:
    """``GET /v1/tournaments/current`` resolves to the active tournament.

    The literal slug ``"current"`` is reserved and never matches a real
    row — it resolves via ``get_tournament``'s active-tournament query.
    External probes (Cloud Monitoring uptime, Sentry uptime) use this
    so checks survive tournament rollovers without an infra redeploy.
    """

    async def test_no_tournaments_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/current")).status_code == 404

    async def test_resolves_to_most_recently_started(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Three tournaments, all started in the past; the one with the
        # latest start_date wins regardless of created_at.
        session.add(make_tournament("old", start_date=datetime(2026, 1, 1, tzinfo=UTC)))
        session.add(make_tournament("newest", start_date=datetime(2026, 5, 1, tzinfo=UTC)))
        session.add(make_tournament("middle", start_date=datetime(2026, 3, 1, tzinfo=UTC)))
        await session.commit()

        response = await client.get("/v1/tournaments/current")
        assert response.status_code == 200
        assert response.json()["slug"] == "newest"

    async def test_prefers_started_over_future_scheduled(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A future tournament has the latest start_date but hasn't
        # started yet — the alias prefers a started tournament even
        # though its start_date is older.
        session.add(make_tournament("active", start_date=datetime(2026, 5, 1, tzinfo=UTC)))
        session.add(make_tournament("upcoming", start_date=datetime(2099, 1, 1, tzinfo=UTC)))
        await session.commit()

        response = await client.get("/v1/tournaments/current")
        assert response.status_code == 200
        assert response.json()["slug"] == "active"

    async def test_falls_back_to_most_recent_when_none_started(
        self, client: AsyncClient, session: AsyncSession
    ):
        # No tournament has a start_date in the past — fall back to
        # the most recently created row. Explicit ``created_at`` is set
        # because SQLite's ``CURRENT_TIMESTAMP`` only has second-
        # precision and rapid commits would tie.
        session.add(make_tournament("first", created_at=datetime(2026, 1, 1, tzinfo=UTC)))
        session.add(
            make_tournament(
                "future",
                start_date=datetime(2099, 1, 1, tzinfo=UTC),
                created_at=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )
        session.add(make_tournament("second", created_at=datetime(2026, 3, 1, tzinfo=UTC)))
        await session.commit()

        response = await client.get("/v1/tournaments/current")
        assert response.status_code == 200
        # ``second`` is the most recently created and resolves over the
        # null-start_date ``first`` row and the future-scheduled one.
        assert response.json()["slug"] == "second"

    async def test_standings_subroute_resolves_via_alias(
        self, client: AsyncClient, session: AsyncSession
    ):
        # The same resolver is shared with every per-tournament read,
        # so ``GET /v1/tournaments/current/standings`` should land on
        # the resolved tournament's roster without any router-side
        # duplication.
        active = make_tournament(
            "active",
            profile_ids=[1],
            start_date=datetime(2026, 5, 1, tzinfo=UTC),
        )
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(active)
        await session.commit()

        response = await client.get("/v1/tournaments/current/standings")
        assert response.status_code == 200
        items = response.json()["items"]
        assert [row["profile_id"] for row in items] == [1]


class TestTournamentStandings:
    async def test_unknown_tournament_returns_404(self, client: AsyncClient):
        assert (await client.get("/v1/tournaments/nope/standings")).status_code == 404

    async def test_empty_roster_returns_empty_envelope(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup"))
        await session.commit()

        response = await client.get("/v1/tournaments/cup/standings")
        assert response.status_code == 200
        assert response.json() == {"last_polled_at": None, "items": []}

    async def test_sorted_by_current_rating_descending(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id, alias, rating in ((1, "low", 1500), (2, "high", 2500), (3, "mid", 2000)):
            player = make_player(profile_id, alias=alias)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=rating)
            )
            session.add(player)
        session.add(make_tournament("cup", profile_ids=[1, 2, 3], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert [row["alias"] for row in items] == ["high", "mid", "low"]
        assert [row["current_rating"] for row in items] == [2500, 2000, 1500]

    async def test_scoped_to_tournament_roster(self, client: AsyncClient, session: AsyncSession):
        # A player with a rating on the leaderboard but outside the roster
        # must not appear in the tournament's standings.
        for profile_id in (1, 2):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=2000)
            )
            session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert [row["profile_id"] for row in items] == [1]

    async def test_scoped_to_tournament_leaderboard(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, alias="solo")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        player.ratings.append(make_player_rating(1, leaderboard_id=4, current_rating=1500))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=4))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert len(items) == 1
        assert items[0]["current_rating"] == 1500

    async def test_row_has_denormalized_player_fields(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(199325, alias="Hera", country="ca")
        player.ratings.append(make_player_rating(199325, leaderboard_id=3, current_rating=2788))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[199325], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["profile_id"] == 199325
        assert row["alias"] == "Hera"
        assert row["country"] == "ca"
        assert row["current_rating"] == 2788

    async def test_cache_control_header_unauthenticated(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Viewer path: `s-maxage=15` lets CF coalesce origin traffic at
        # event-window scale (docs/event-traffic-cost-model.md);
        # `max-age=0, must-revalidate` forces the browser to always
        # check (#96).
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/standings")
        assert (
            response.headers["Cache-Control"] == "public, s-maxage=15, max-age=0, must-revalidate"
        )
        assert "Cookie" not in response.headers.get("Vary", "")

    async def test_cache_control_header_authenticated(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Admin path: cookie presence flips Cache-Control to
        # `private, no-store` so the admin's read-after-write skips
        # every cache layer (#105). The cookie need only be present;
        # the helper does not verify the JWT.
        session.add(make_tournament("cup"))
        await session.commit()
        client.cookies.set("criticalbit_access", "any-value")
        response = await client.get("/v1/tournaments/cup/standings")
        assert response.headers["Cache-Control"] == "private, no-store"
        assert "Cookie" not in response.headers.get("Vary", "")

    async def test_team_folded_onto_standings_rows(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Two roster players: one on a team, one on none. The teamed player's
        # row carries the team's id + display strings; the other carries null.
        for profile_id, rating in ((1, 2500), (2, 2400)):
            player = make_player(profile_id, alias=f"p{profile_id}")
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=rating)
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        tournament.teams = [make_team(tournament, "Grubby", profile_ids=[1], initials="G")]
        session.add(tournament)
        await session.commit()

        rows = {
            row["profile_id"]: row
            for row in (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        }
        assert rows[2]["team"] is None
        grubby = rows[1]["team"]
        assert grubby is not None
        assert set(grubby) == {"team_id", "name", "initials"}
        assert grubby["name"] == "Grubby"
        assert grubby["initials"] == "G"
        assert isinstance(grubby["team_id"], int)

    async def test_placeholder_member_row_carries_team(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A placeholder roster slot (no profile_id minted yet) that's on a
        # team surfaces the team chip on its standings row — the live Jabo
        # bug (#187). team_members keys on tournament_player_id (#181), so a
        # placeholder can be teamed; the standings read used to key the team
        # map on profile_id and hardcode team=None for placeholders, so the
        # chip never reached the row.
        tournament = make_tournament("cup", leaderboard_id=3)
        tournament.tracked_players.append(TournamentPlayer(name="Jabo"))
        tournament.teams = [make_team(tournament, "PiG", placeholder_names=["Jabo"], initials="PG")]
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert len(items) == 1
        jabo = items[0]
        assert jabo["profile_id"] is None
        assert jabo["alias"] == "Jabo"
        team = jabo["team"]
        assert team is not None
        assert set(team) == {"team_id", "name", "initials"}
        assert team["name"] == "PiG"
        assert team["initials"] == "PG"
        assert isinstance(team["team_id"], int)

    async def test_presentation_folded_onto_standings_rows(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A roster player with a presentation bag set carries it on their
        # row; a player with none carries an empty object.
        for profile_id, rating in ((1, 2500), (2, 2400)):
            player = make_player(profile_id, alias=f"p{profile_id}")
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=rating)
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        for tracked in tournament.tracked_players:
            if tracked.profile_id == 1:
                tracked.presentation = {"streamUrls": ["https://twitch.tv/p1"]}
        session.add(tournament)
        await session.commit()

        rows = {
            row["profile_id"]: row
            for row in (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        }
        assert rows[1]["presentation"] == {"streamUrls": ["https://twitch.tv/p1"]}
        assert rows[2]["presentation"] == {}


class TestStandingsUnratedRoster:
    """Roster members without a rating on the tournament's leaderboard still surface."""

    async def test_unrated_member_appears_in_tail_with_null_fields(
        self, client: AsyncClient, session: AsyncSession
    ):
        # One rated player + one rosterless-rating player. The unrated one
        # surfaces after the rated one with current_rating / max_rating null,
        # zero wins/losses/streak, empty recent_results, and a zero record.
        rated = make_player(1, alias="rated")
        rated.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(rated)
        session.add(make_player(2, alias="newbie"))
        session.add(make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert [row["profile_id"] for row in items] == [1, 2]
        newbie = items[1]
        assert newbie["current_rating"] is None
        assert newbie["max_rating"] is None
        assert newbie["wins"] == 0
        assert newbie["losses"] == 0
        assert newbie["streak"] == 0
        assert newbie["recent_results"] == []
        assert newbie["tournament_record"] == {
            "games_played": 0,
            "wins": 0,
            "losses": 0,
            "streak": 0,
            "peak_rating": None,
            "last_match_at": None,
            "recent_results": [],
            "recent_matchups": [],
            "win_pct": None,
        }
        assert newbie["rank"] is None
        assert newbie["rank_total"] is None
        assert newbie["last_match_at"] is None
        # Derived fields fall out correctly for the zero case.
        assert newbie["games"] == 0
        assert newbie["win_pct"] is None

    async def test_only_rating_on_other_leaderboard_is_unrated(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A roster member with a rating on a *different* leaderboard than the
        # tournament's still surfaces on this standings as unrated — not
        # silently dropped, and not borrowing the other leaderboard's rating.
        player = make_player(1, alias="solo")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=4))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert len(items) == 1
        assert items[0]["profile_id"] == 1
        assert items[0]["current_rating"] is None

    async def test_unrated_sorted_by_display_name(self, client: AsyncClient, session: AsyncSession):
        # Three unrated members + one rated. The unrated rows are ordered by
        # display name ASC (#187 — was profile_id), so the public list is
        # deterministic. Aliases are chosen to differ from profile_id order:
        # name order [Alice, Bob, Charlie] ≠ profile_id order [10, 20, 30].
        rated = make_player(50, alias="rated")
        rated.ratings.append(make_player_rating(50, leaderboard_id=3, current_rating=2000))
        session.add(rated)
        for profile_id, alias in ((10, "Charlie"), (20, "Alice"), (30, "Bob")):
            session.add(make_player(profile_id, alias=alias))
        tournament = make_tournament("cup", profile_ids=[10, 20, 30, 50], leaderboard_id=3)
        # Roster name = alias (mirrors the prod backfill); standings sort by name.
        for tp in tournament.tracked_players:
            tp.name = {10: "Charlie", 20: "Alice", 30: "Bob", 50: "rated"}[tp.profile_id]
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert [row["alias"] for row in items] == ["rated", "Alice", "Bob", "Charlie"]
        assert [row["profile_id"] for row in items] == [50, 20, 30, 10]


class TestStandingsUnlinkedRows:
    """Unlinked roster rows sort among the unrated by display name (#187)."""

    async def test_unlinked_sorts_among_unrated_by_name(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Rated + unrated + unlinked on one tournament. Rated first (by
        # rating DESC); then every unrated row — linked or not — by display
        # name. "iyouxin" (unlinked) sorts before "newbie" (unrated), NOT to
        # a separate tail: #187 dropped the placeholder-tail special case.
        rated = make_player(10, alias="rated")
        rated.ratings.append(make_player_rating(10, leaderboard_id=3, current_rating=2000))
        session.add(rated)
        session.add(make_player(20, alias="newbie"))
        tournament = make_tournament("cup", profile_ids=[10, 20], leaderboard_id=3)
        tournament.tracked_players.append(
            TournamentPlayer(name="iyouxin", presentation={"flag": "🇺🇦"})
        )
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert [row["alias"] for row in items] == ["rated", "iyouxin", "newbie"]
        ghost = next(row for row in items if row["profile_id"] is None)
        assert ghost["profile_id"] is None
        assert ghost["name"] == "iyouxin"
        assert ghost["alias"] == "iyouxin"
        assert ghost["country"] is None
        assert ghost["team"] is None
        assert ghost["presentation"] == {"flag": "🇺🇦"}
        assert ghost["current_rating"] is None
        assert ghost["max_rating"] is None
        assert ghost["wins"] == 0
        assert ghost["losses"] == 0
        assert ghost["streak"] == 0
        assert ghost["recent_results"] == []
        assert ghost["tournament_record"] == {
            "games_played": 0,
            "wins": 0,
            "losses": 0,
            "streak": 0,
            "peak_rating": None,
            "last_match_at": None,
            "recent_results": [],
            "recent_matchups": [],
            "win_pct": None,
        }
        assert ghost["rank"] is None
        assert ghost["rank_total"] is None
        assert ghost["in_match"] is False
        assert ghost["live_match_id"] is None
        assert ghost["stream_live"] is False
        assert ghost["last_match_at"] is None
        assert ghost["updated_at"] is None
        assert ghost["games"] == 0
        assert ghost["win_pct"] is None

    async def test_rows_expose_tournament_player_id(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Every standings row — rated, unrated, and placeholder — carries its
        # roster-row surrogate id (#167) so the FE can drive team assignment
        # straight off the standings without a separate lookup.
        rated = make_player(10, alias="rated")
        rated.ratings.append(make_player_rating(10, leaderboard_id=3, current_rating=2000))
        session.add(rated)
        tournament = make_tournament("cup", profile_ids=[10], leaderboard_id=3)
        tournament.tracked_players.append(TournamentPlayer(name="iyouxin"))
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
        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert all(isinstance(row["tournament_player_id"], int) for row in items)
        assert {row["tournament_player_id"] for row in items} == expected_ids

    async def test_unlinked_only_sorted_alphabetically(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Three unlinked rows, no linked players. Sorted by display name ASC.
        tournament = make_tournament("cup")
        tournament.tracked_players = [
            TournamentPlayer(name="Zeke"),
            TournamentPlayer(name="Alice"),
            TournamentPlayer(name="Marco"),
        ]
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert [row["alias"] for row in items] == ["Alice", "Marco", "Zeke"]
        assert [row["name"] for row in items] == ["Alice", "Marco", "Zeke"]

    async def test_placeholder_only_returns_three_rows(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A tournament with placeholders but no real roster members still
        # renders a populated standings — important for the "announced before
        # anyone plays" announcement-window state.
        tournament = make_tournament("cup")
        tournament.tracked_players = [
            TournamentPlayer(name=n) for n in ("iyouxin", "Jabo", "Gunnar")
        ]
        session.add(tournament)
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert len(items) == 3
        assert all(row["profile_id"] is None for row in items)


class TestStandingsDerivedFields:
    """games / win_pct: derived server-side from each row's lifetime wins/losses."""

    async def test_games_and_win_pct_derived_from_wins_losses(
        self, client: AsyncClient, session: AsyncSession
    ):
        # 2-1 record: games = 3, win_pct = 66.666… rounded to 1 dp = 66.7.
        player = make_player(1, alias="p1")
        player.ratings.append(
            make_player_rating(1, leaderboard_id=3, current_rating=2000, wins=2, losses=1)
        )
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["games"] == 3
        assert row["win_pct"] == 66.7

    async def test_win_pct_null_when_no_games(self, client: AsyncClient, session: AsyncSession):
        # A rostered player who hasn't played: games = 0, win_pct = null
        # (rather than a misleading 0.0) so the consumer can render "—".
        player = make_player(1, alias="p1")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=0))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["games"] == 0
        assert row["win_pct"] is None


class TestStandingsRecentResults:
    """recent_results: per-player recent win/loss form on the standings row."""

    async def test_most_recent_first(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1, alias="hera")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, day, outcome in (
            (101, 1, MatchOutcome.LOSS),
            (102, 2, MatchOutcome.LOSS),
            (103, 3, MatchOutcome.WIN),
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(make_match_player(match_id, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["recent_results"] == ["win", "loss", "loss"]

    async def test_capped_at_limit_keeping_most_recent(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # One match per day; the oldest (day 1) is the only LOSS. With more
        # matches than the cap, that oldest row must fall outside the window.
        total = _RECENT_RESULTS_LIMIT + 5
        for day in range(1, total + 1):
            outcome = MatchOutcome.LOSS if day == 1 else MatchOutcome.WIN
            match = make_match(
                100 + day,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(make_match_player(100 + day, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        results = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0][
            "recent_results"
        ]
        assert len(results) == _RECENT_RESULTS_LIMIT
        assert results == ["win"] * _RECENT_RESULTS_LIMIT

    async def test_scoped_to_tournament_leaderboard(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)

        on_lb3 = make_match(201, leaderboard_id=3)
        on_lb3.players.append(make_match_player(201, profile_id=1, outcome=MatchOutcome.WIN))
        on_lb4 = make_match(202, leaderboard_id=4)
        on_lb4.players.append(make_match_player(202, profile_id=1, outcome=MatchOutcome.LOSS))
        no_lb = make_match(203, leaderboard_id=None)
        no_lb.players.append(make_match_player(203, profile_id=1, outcome=MatchOutcome.LOSS))
        session.add_all([on_lb3, on_lb4, no_lb])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["recent_results"] == ["win"]

    async def test_excludes_in_progress_matches(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)

        completed = make_match(
            301, leaderboard_id=3, started_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        )
        completed.players.append(make_match_player(301, profile_id=1, outcome=MatchOutcome.WIN))
        # More recent, but still in progress — null outcome, must be skipped.
        live = make_match(
            302,
            leaderboard_id=3,
            state=MatchState.IN_PROGRESS,
            completed_at=None,
            started_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )
        live.players.append(make_match_player(302, profile_id=1, outcome=None))
        session.add_all([completed, live])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["recent_results"] == ["win"]

    async def test_empty_when_player_has_no_matches(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["recent_results"] == []

    async def test_per_player_buckets_are_isolated(
        self, client: AsyncClient, session: AsyncSession
    ):
        one = make_player(1, alias="one")
        one.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2200))
        two = make_player(2, alias="two")
        two.ratings.append(make_player_rating(2, leaderboard_id=3, current_rating=2100))
        session.add_all([one, two])

        for match_id, day, profile_id, outcome in (
            (401, 1, 1, MatchOutcome.LOSS),
            (402, 2, 1, MatchOutcome.WIN),
            (403, 1, 2, MatchOutcome.WIN),
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(match_id, profile_id=profile_id, outcome=outcome)
            )
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        results_by_profile = {row["profile_id"]: row["recent_results"] for row in items}
        assert results_by_profile == {1: ["win", "loss"], 2: ["win"]}


class TestStandingsInMatch:
    """in_match / live_match_id: live-match status on the standings row."""

    async def test_in_match_true_with_live_match_id(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_match(501, state=MatchState.IN_PROGRESS, completed_at=None))
        session.add(LiveMatchPlayer(match_id=501, profile_id=1))
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["in_match"] is True
        assert row["live_match_id"] == 501

    async def test_not_in_match_when_no_live_row(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["in_match"] is False
        assert row["live_match_id"] is None

    async def test_staging_match_counts_as_in_match(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_match(502, state=MatchState.STAGING, completed_at=None))
        session.add(LiveMatchPlayer(match_id=502, profile_id=1))
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["in_match"] is True
        assert row["live_match_id"] == 502

    async def test_completed_match_does_not_count(self, client: AsyncClient, session: AsyncSession):
        # A stale live_match_players row pointing at a match the recent feed
        # has already flipped to completed must not show in_match.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_match(503, state=MatchState.COMPLETED))
        session.add(LiveMatchPlayer(match_id=503, profile_id=1))
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["in_match"] is False
        assert row["live_match_id"] is None

    async def test_in_match_is_per_player(self, client: AsyncClient, session: AsyncSession):
        one = make_player(1, alias="one")
        one.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2200))
        two = make_player(2, alias="two")
        two.ratings.append(make_player_rating(2, leaderboard_id=3, current_rating=2100))
        session.add_all([one, two])
        session.add(make_match(504, state=MatchState.IN_PROGRESS, completed_at=None))
        session.add(LiveMatchPlayer(match_id=504, profile_id=1))
        session.add(make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        status = {r["profile_id"]: (r["in_match"], r["live_match_id"]) for r in items}
        assert status == {1: (True, 504), 2: (False, None)}


class TestStandingsTournamentRecord:
    """tournament_record: games / wins / losses / streak within the window."""

    async def test_counts_wins_losses_and_games(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, outcome in (
            (1, MatchOutcome.WIN),
            (2, MatchOutcome.WIN),
            (3, MatchOutcome.LOSS),
        ):
            match = make_match(match_id, leaderboard_id=3)
            match.players.append(make_match_player(match_id, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["games_played"] == 3
        assert row["tournament_record"]["wins"] == 2
        assert row["tournament_record"]["losses"] == 1

    async def test_win_streak_is_positive(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # Oldest -> newest: LOSS, WIN, WIN — current streak is +2.
        for match_id, day, outcome in (
            (1, 1, MatchOutcome.LOSS),
            (2, 2, MatchOutcome.WIN),
            (3, 3, MatchOutcome.WIN),
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(make_match_player(match_id, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["streak"] == 2

    async def test_loss_streak_is_negative(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # Oldest -> newest: WIN, LOSS, LOSS — current streak is -2.
        for match_id, day, outcome in (
            (1, 1, MatchOutcome.WIN),
            (2, 2, MatchOutcome.LOSS),
            (3, 3, MatchOutcome.LOSS),
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(make_match_player(match_id, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["streak"] == -2

    async def test_excludes_matches_outside_the_window(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, day in ((1, 3), (2, 7), (3, 12)):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(match_id, profile_id=1, outcome=MatchOutcome.WIN)
            )
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1],
                leaderboard_id=3,
                start_date=datetime(2026, 5, 5, tzinfo=UTC),
                grand_finals_date=datetime(2026, 5, 10, tzinfo=UTC),
            )
        )
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["games_played"] == 1

    async def test_null_window_counts_all_matches(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id in (1, 2):
            match = make_match(match_id, leaderboard_id=3)
            match.players.append(
                make_match_player(match_id, profile_id=1, outcome=MatchOutcome.WIN)
            )
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["games_played"] == 2

    async def test_zero_record_when_no_matches(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"] == {
            "games_played": 0,
            "wins": 0,
            "losses": 0,
            "streak": 0,
            "peak_rating": None,
            "last_match_at": None,
            "recent_results": [],
            "recent_matchups": [],
            "win_pct": None,
        }

    async def test_peak_rating_is_max_in_window(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, day, new_rating in (
            (1, 1, 1900),
            (2, 2, 2050),  # peak
            (3, 3, 1980),
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(
                    match_id, profile_id=1, outcome=MatchOutcome.WIN, new_rating=new_rating
                )
            )
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["peak_rating"] == 2050

    async def test_peak_rating_ignores_matches_outside_the_window(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A match with the highest rating sits outside the window — it must
        # not surface as the in-window peak.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, day, new_rating in (
            (1, 3, 1950),  # before window
            (2, 7, 2000),  # in window
            (3, 12, 2100),  # after window — higher, but should not count
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(
                    match_id, profile_id=1, outcome=MatchOutcome.WIN, new_rating=new_rating
                )
            )
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1],
                leaderboard_id=3,
                start_date=datetime(2026, 5, 5, tzinfo=UTC),
                grand_finals_date=datetime(2026, 5, 10, tzinfo=UTC),
            )
        )
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["peak_rating"] == 2000

    async def test_peak_rating_null_when_all_in_window_new_ratings_are_null(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Counts a game (so games_played > 0) but no rating data on any of
        # the in-window matches — peak_rating must report null, not 0.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        match = make_match(1, leaderboard_id=3)
        match.players.append(
            make_match_player(1, profile_id=1, outcome=MatchOutcome.WIN, new_rating=None)
        )
        session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["games_played"] == 1
        assert row["tournament_record"]["peak_rating"] is None

    async def test_last_match_at_is_newest_in_window(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Three matches inside the window — last_match_at picks the newest.
        # A fourth match sits after the window and must be ignored even
        # though it would otherwise be the newer of the lot.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, day in ((1, 6), (2, 8), (3, 9), (4, 15)):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(match_id, profile_id=1, outcome=MatchOutcome.WIN)
            )
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1],
                leaderboard_id=3,
                start_date=datetime(2026, 5, 5, tzinfo=UTC),
                grand_finals_date=datetime(2026, 5, 10, tzinfo=UTC),
            )
        )
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        # SQLite strips the tz on read-back; prod (Postgres) keeps it. Match
        # on the ISO prefix so the assertion is portable across both.
        assert row["tournament_record"]["last_match_at"].startswith("2026-05-09T12:00:00")

    async def test_recent_results_newest_first_capped_at_limit(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Twelve completed matches — recent_results returns the 10 newest
        # outcomes, newest-first.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id in range(1, 13):
            # Match N is on day N (later N = newer). Outcomes alternate.
            outcome = MatchOutcome.WIN if match_id % 2 == 0 else MatchOutcome.LOSS
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, match_id, 12, 0, tzinfo=UTC),
            )
            match.players.append(make_match_player(match_id, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        recent = row["tournament_record"]["recent_results"]
        assert len(recent) == 10
        # Matches 12..3 (newest first): even=win, odd=loss.
        expected = ["win" if mid % 2 == 0 else "loss" for mid in range(12, 2, -1)]
        assert recent == expected

    async def test_recent_results_window_scoped(self, client: AsyncClient, session: AsyncSession):
        # Same-leaderboard matches outside the window are excluded — even
        # when they would otherwise be newer than every in-window match.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, day, outcome in (
            (1, 3, MatchOutcome.WIN),  # before window — excluded
            (2, 6, MatchOutcome.WIN),  # in window
            (3, 8, MatchOutcome.LOSS),  # in window
            (4, 15, MatchOutcome.LOSS),  # after window — excluded
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
            )
            match.players.append(make_match_player(match_id, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1],
                leaderboard_id=3,
                start_date=datetime(2026, 5, 5, tzinfo=UTC),
                grand_finals_date=datetime(2026, 5, 10, tzinfo=UTC),
            )
        )
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["recent_results"] == ["loss", "win"]

    async def test_win_pct_rounds_to_one_decimal(self, client: AsyncClient, session: AsyncSession):
        # 2 wins of 3 → 66.66...% → rounds to 66.7.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id, outcome in (
            (1, MatchOutcome.WIN),
            (2, MatchOutcome.WIN),
            (3, MatchOutcome.LOSS),
        ):
            match = make_match(match_id, leaderboard_id=3)
            match.players.append(make_match_player(match_id, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["tournament_record"]["win_pct"] == 66.7


class TestStandingsRecentMatchups:
    """recent_matchups: tournament_record recent games enriched with the civ matchup (#218)."""

    async def test_carries_civ_matchup_newest_first(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # Two completed games; the entrant (profile 1, team 0) faces a ladder
        # opponent (profile 999, team 1). Day 3 is newer than day 2.
        for match_id, day, entrant_civ, opp_civ, outcome, map_name in (
            (1, 2, 27, 7, MatchOutcome.WIN, "Arabia.rms"),
            (2, 3, 13, 99, MatchOutcome.LOSS, "Arena.rms"),
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                map_name=map_name,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
                completed_at=datetime(2026, 5, day, 12, 30, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(
                    match_id, profile_id=1, team_id=0, civilization_id=entrant_civ, outcome=outcome
                )
            )
            match.players.append(
                make_match_player(match_id, profile_id=999, team_id=1, civilization_id=opp_civ)
            )
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        record = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0][
            "tournament_record"
        ]
        matchups = record["recent_matchups"]
        # Newest-first: day-3 (Arena, loss, 13 vs 99) then day-2 (Arabia, win, 27 vs 7).
        assert [m["outcome"] for m in matchups] == ["loss", "win"]
        assert [m["civilization_id"] for m in matchups] == [13, 27]
        assert [m["opponent_civilization_id"] for m in matchups] == [99, 7]
        assert [m["map_name"] for m in matchups] == ["Arena.rms", "Arabia.rms"]
        # Suffix (Z vs +00:00) varies by environment; assert on the ISO prefix.
        assert matchups[0]["completed_at"].startswith("2026-05-03T12:30:00")
        assert matchups[1]["completed_at"].startswith("2026-05-02T12:30:00")

    async def test_opponent_civ_null_when_no_opposing_row(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A match record with only the entrant's row (no opposing-team player)
        # keeps the matchup but leaves the opponent civ null.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        match = make_match(1, leaderboard_id=3)
        match.players.append(
            make_match_player(
                1, profile_id=1, team_id=0, civilization_id=27, outcome=MatchOutcome.WIN
            )
        )
        session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        matchups = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0][
            "tournament_record"
        ]["recent_matchups"]
        assert len(matchups) == 1
        assert matchups[0]["civilization_id"] == 27
        assert matchups[0]["opponent_civilization_id"] is None

    async def test_parity_with_recent_results_cap_and_window(
        self, client: AsyncClient, session: AsyncSession
    ):
        # recent_matchups is the same row set as recent_results: same cap (10),
        # same newest-first order, same window scoping.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # 12 in-window games (civ id == match id == day) + 1 pre-window.
        for match_id in range(1, 13):
            outcome = MatchOutcome.WIN if match_id % 2 == 0 else MatchOutcome.LOSS
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, match_id, 12, 0, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(
                    match_id, profile_id=1, team_id=0, civilization_id=match_id, outcome=outcome
                )
            )
            match.players.append(
                make_match_player(match_id, profile_id=999, team_id=1, civilization_id=0)
            )
            session.add(match)
        pre = make_match(99, leaderboard_id=3, started_at=datetime(2020, 1, 1, 12, 0, tzinfo=UTC))
        pre.players.append(make_match_player(99, profile_id=1, team_id=0, civilization_id=5))
        session.add(pre)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1],
                leaderboard_id=3,
                start_date=datetime(2026, 5, 1, tzinfo=UTC),
                grand_finals_date=datetime(2026, 5, 31, tzinfo=UTC),
            )
        )
        await session.commit()

        record = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0][
            "tournament_record"
        ]
        results = record["recent_results"]
        matchups = record["recent_matchups"]
        assert len(matchups) == len(results) == _RECENT_RESULTS_LIMIT  # capped, pre-window dropped
        assert [m["outcome"] for m in matchups] == results
        # civ id == match id here; newest-first = matches 12..3.
        assert [m["civilization_id"] for m in matchups] == list(range(12, 2, -1))


class TestUpdateTournament:
    """PATCH /v1/tournaments/{slug} — owner-gated metadata edits."""

    async def test_owner_updates_name(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", name="Old Name", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"name": "New Name"})
        assert response.status_code == 200
        assert response.json()["name"] == "New Name"
        # The change is persisted, not just echoed back.
        assert (await client.get("/v1/tournaments/cup")).json()["name"] == "New Name"

    async def test_partial_update_leaves_other_fields_untouched(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(
            make_tournament(
                "cup", name="Keep Me", leaderboard_id=3, owner_ids=[DEFAULT_TEST_USER_ID]
            )
        )
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"leaderboard_id": 4})
        assert response.status_code == 200
        body = response.json()
        assert body["leaderboard_id"] == 4
        assert body["name"] == "Keep Me"

    async def test_can_clear_a_date_with_null(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(
            make_tournament(
                "cup",
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
                owner_ids=[DEFAULT_TEST_USER_ID],
            )
        )
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"start_date": None})
        assert response.status_code == 200
        assert response.json()["start_date"] is None

    async def test_can_set_grand_finals_date(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup",
            json={"grand_finals_date": "2026-06-15T18:00:00Z"},
        )
        assert response.status_code == 200
        assert response.json()["grand_finals_date"].startswith("2026-06-15T18")

    async def test_can_clear_grand_finals_date_with_null(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(
            make_tournament(
                "cup",
                grand_finals_date=datetime(2026, 6, 15, 18, tzinfo=UTC),
                owner_ids=[DEFAULT_TEST_USER_ID],
            )
        )
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"grand_finals_date": None})
        assert response.status_code == 200
        assert response.json()["grand_finals_date"] is None

    async def test_can_set_prize_pool_cents(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup",
            json={"prize_pool_cents": 512750},
        )
        assert response.status_code == 200
        assert response.json()["prize_pool_cents"] == 512750

    async def test_can_clear_prize_pool_cents_with_null(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(
            make_tournament(
                "cup",
                prize_pool_cents=512750,
                owner_ids=[DEFAULT_TEST_USER_ID],
            )
        )
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"prize_pool_cents": None})
        assert response.status_code == 200
        assert response.json()["prize_pool_cents"] is None

    async def test_negative_prize_pool_cents_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"prize_pool_cents": -1})
        assert response.status_code == 422

    async def test_can_set_host_stream_urls(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup",
            json={
                "host_stream_urls": [
                    "https://twitch.tv/host",
                    "https://youtube.com/@host",
                ]
            },
        )
        assert response.status_code == 200
        assert response.json()["host_stream_urls"] == [
            "https://twitch.tv/host",
            "https://youtube.com/@host",
        ]

    async def test_can_clear_host_stream_urls_with_empty_list(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # Empty list is the "host detection off" state — distinct from
        # explicit null (which is rejected; this isn't a nullable column).
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(
            make_tournament(
                "cup",
                host_stream_urls=["https://twitch.tv/host"],
                owner_ids=[DEFAULT_TEST_USER_ID],
            )
        )
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"host_stream_urls": []})
        assert response.status_code == 200
        assert response.json()["host_stream_urls"] == []

    async def test_explicit_null_host_stream_urls_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"host_stream_urls": None})
        assert response.status_code == 422

    async def test_too_many_host_stream_urls_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup",
            json={"host_stream_urls": [f"https://twitch.tv/h{i}" for i in range(6)]},
        )
        assert response.status_code == 422

    async def test_empty_host_stream_url_string_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"host_stream_urls": [""]})
        assert response.status_code == 422

    async def test_empty_body_is_a_noop(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", name="Unchanged", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={})
        assert response.status_code == 200
        assert response.json()["name"] == "Unchanged"

    async def test_start_after_grand_finals_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch(
            "/v1/tournaments/cup",
            json={
                "start_date": "2026-07-01T00:00:00Z",
                "grand_finals_date": "2026-06-01T00:00:00Z",
            },
        )
        assert response.status_code == 422

    async def test_explicit_null_name_is_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.patch("/v1/tournaments/cup", json={"name": None})
        assert response.status_code == 422


class TestCreateTournament:
    """POST /v1/tournaments — self-serve create, caller becomes owner."""

    _BODY = {
        "slug": "spring-cup",
        "name": "Spring Cup",
        "leaderboard_id": 3,
    }

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        response = await client.post("/v1/tournaments", json=self._BODY)
        assert response.status_code == 401

    async def test_creates_tournament_and_returns_201(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post("/v1/tournaments", json=self._BODY)
        assert response.status_code == 201
        body = response.json()
        assert body["slug"] == "spring-cup"
        assert body["name"] == "Spring Cup"
        assert body["leaderboard_id"] == 3
        # Persisted, not just echoed back.
        get_response = await client.get("/v1/tournaments/spring-cup")
        assert get_response.status_code == 200
        assert get_response.json()["slug"] == "spring-cup"

    async def test_creator_becomes_owner(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        await client.post("/v1/tournaments", json=self._BODY)
        # The caller can immediately PATCH the new tournament — proves
        # the owner row landed under their user id.
        response = await client.patch("/v1/tournaments/spring-cup", json={"name": "Renamed"})
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    async def test_optional_dates_round_trip(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post(
            "/v1/tournaments",
            json={
                **self._BODY,
                "start_date": "2026-06-01T00:00:00Z",
                "grand_finals_date": "2026-06-15T18:00:00Z",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["start_date"].startswith("2026-06-01")
        assert body["grand_finals_date"].startswith("2026-06-15T18")

    async def test_optional_prize_pool_cents_round_trips(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post(
            "/v1/tournaments",
            json={**self._BODY, "prize_pool_cents": 512750},
        )
        assert response.status_code == 201
        assert response.json()["prize_pool_cents"] == 512750

    async def test_negative_prize_pool_cents_on_create_returns_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post(
            "/v1/tournaments",
            json={**self._BODY, "prize_pool_cents": -1},
        )
        assert response.status_code == 422

    async def test_optional_host_stream_urls_round_trips(self, client: AsyncClient, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post(
            "/v1/tournaments",
            json={
                **self._BODY,
                "host_stream_urls": ["https://twitch.tv/host"],
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["host_stream_urls"] == ["https://twitch.tv/host"]
        # Brand-new tournament can't be in host_live_streams yet.
        assert body["host_stream_live"] is False

    async def test_host_stream_urls_default_to_empty_on_create(self, client: AsyncClient, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post("/v1/tournaments", json=self._BODY)
        assert response.status_code == 201
        assert response.json()["host_stream_urls"] == []

    async def test_too_many_host_stream_urls_on_create_returns_422(
        self, client: AsyncClient, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post(
            "/v1/tournaments",
            json={
                **self._BODY,
                "host_stream_urls": [f"https://twitch.tv/h{i}" for i in range(6)],
            },
        )
        assert response.status_code == 422

    async def test_duplicate_slug_returns_409(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("spring-cup"))
        await session.commit()

        response = await client.post("/v1/tournaments", json=self._BODY)
        assert response.status_code == 409

    async def test_invalid_slug_format_returns_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        # Uppercase, spaces, leading/trailing hyphens are all rejected.
        for bad_slug in ("Spring Cup", "-leading", "trailing-", "Caps"):
            response = await client.post("/v1/tournaments", json={**self._BODY, "slug": bad_slug})
            assert response.status_code == 422, f"expected 422 for slug={bad_slug!r}"

    async def test_reserved_current_slug_returns_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # ``current`` is the active-tournament alias resolved by
        # ``get_tournament``; allowing a literal row with that slug
        # would make the row unreachable and ambiguous.
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post("/v1/tournaments", json={**self._BODY, "slug": "current"})
        assert response.status_code == 422

    async def test_start_after_grand_finals_returns_422(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.post(
            "/v1/tournaments",
            json={
                **self._BODY,
                "start_date": "2026-07-01T00:00:00Z",
                "grand_finals_date": "2026-06-01T00:00:00Z",
            },
        )
        assert response.status_code == 422


class TestDeleteTournament:
    """DELETE /v1/tournaments/{slug} — owner-gated, cascades to scoped rows."""

    async def test_unauthenticated_returns_401(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup"))
        await session.commit()

        response = await client.delete("/v1/tournaments/cup")
        assert response.status_code == 401

    async def test_unknown_slug_returns_404(self, client: AsyncClient, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete("/v1/tournaments/nope")
        assert response.status_code == 404

    async def test_non_owner_returns_403(self, client: AsyncClient, session: AsyncSession, auth_as):
        other_user = "00000000-0000-0000-0000-0000000000bb"
        session.add(make_tournament("cup", owner_ids=[other_user]))
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete("/v1/tournaments/cup")
        assert response.status_code == 403

    async def test_owner_deletes(self, client: AsyncClient, session: AsyncSession, auth_as):
        auth_as(DEFAULT_TEST_USER_ID)
        session.add(make_tournament("cup", owner_ids=[DEFAULT_TEST_USER_ID]))
        await session.commit()

        response = await client.delete("/v1/tournaments/cup")
        assert response.status_code == 204
        assert response.content == b""
        assert (await client.get("/v1/tournaments/cup")).status_code == 404

    async def test_cascades_to_roster_teams_and_owners(
        self, client: AsyncClient, session: AsyncSession, auth_as
    ):
        # A tournament with a roster, teams (with members), and a second
        # owner — every scoped row should be gone after the delete.
        tournament = make_tournament(
            "cup",
            profile_ids=[1, 2],
            owner_ids=[DEFAULT_TEST_USER_ID, "00000000-0000-0000-0000-0000000000bb"],
        )
        team = make_team(tournament, "Reds", profile_ids=[1])
        tournament.teams = [team]
        session.add(tournament)
        await session.commit()

        auth_as(DEFAULT_TEST_USER_ID)
        response = await client.delete("/v1/tournaments/cup")
        assert response.status_code == 204

        # Tournament row gone — every scoped row cascades with it.
        assert (
            await session.execute(select(Tournament).where(Tournament.slug == "cup"))
        ).scalar_one_or_none() is None
        assert (await session.execute(select(TournamentPlayer))).first() is None
        assert (await session.execute(select(Team))).first() is None
        assert (await session.execute(select(TeamMember))).first() is None
        assert (await session.execute(select(TournamentOwner))).first() is None


class TestProgression:
    """GET /{slug}/progression: per-player rating-over-time series."""

    async def test_per_player_series_oldest_first(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1, alias="hera")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=1530))
        session.add(player)
        # Three completed matches, ascending dates and post-match ratings.
        for match_id, day, rating in ((101, 1, 1490), (102, 2, 1510), (103, 3, 1530)):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
                completed_at=datetime(2026, 5, day, 12, 30, tzinfo=UTC),
            )
            match.players.append(make_match_player(match_id, profile_id=1, new_rating=rating))
            session.add(match)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        session.add(tournament)
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/progression")).json()
        assert len(body["items"]) == 1
        series = body["items"][0]
        # The stable per-series key (#187) maps to the roster row.
        assert series["tournament_player_id"] == tournament.tracked_players[0].id
        assert series["profile_id"] == 1
        assert series["alias"] == "hera"
        assert [p["rating"] for p in series["points"]] == [1490, 1510, 1530]
        # ISO-8601 UTC strings sort lexicographically = chronologically.
        completed = [p["completed_at"] for p in series["points"]]
        assert completed == sorted(completed)
        assert body["last_polled_at"] is not None

    async def test_windowed_to_tournament_dates(self, client: AsyncClient, session: AsyncSession):
        # Only matches whose started_at falls inside [start_date, grand_finals_date]
        # contribute points — a years-old match and one past the window are dropped,
        # mirroring tournament_record so the chart reflects in-event movement, not a
        # player's whole tracked history.
        player = make_player(1, alias="hera")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=1530))
        session.add(player)
        # (match_id, started_at, completed_at, new_rating): pre-window (2021),
        # two in-window (2026-06), post-window (2026-07).
        matches = [
            (
                101,
                datetime(2021, 3, 1, 12, 0, tzinfo=UTC),
                datetime(2021, 3, 1, 12, 30, tzinfo=UTC),
                1400,
            ),
            (
                102,
                datetime(2026, 6, 2, 12, 0, tzinfo=UTC),
                datetime(2026, 6, 2, 12, 30, tzinfo=UTC),
                1490,
            ),
            (
                103,
                datetime(2026, 6, 9, 12, 0, tzinfo=UTC),
                datetime(2026, 6, 9, 12, 30, tzinfo=UTC),
                1520,
            ),
            (
                104,
                datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
                datetime(2026, 7, 1, 12, 30, tzinfo=UTC),
                1600,
            ),
        ]
        for match_id, started_at, completed_at, rating in matches:
            match = make_match(
                match_id, leaderboard_id=3, started_at=started_at, completed_at=completed_at
            )
            match.players.append(make_match_player(match_id, profile_id=1, new_rating=rating))
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1],
                leaderboard_id=3,
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
                grand_finals_date=datetime(2026, 6, 30, tzinfo=UTC),
            )
        )
        await session.commit()

        series = (await client.get("/v1/tournaments/cup/progression")).json()["items"][0]
        # Pre-window (1400) and post-window (1600) dropped; only in-window points remain.
        assert [p["rating"] for p in series["points"]] == [1490, 1520]

    async def test_scoped_to_tournament_leaderboard(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A match on another leaderboard contributes no points.
        player = make_player(1, alias="hera")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=1500))
        session.add(player)
        on = make_match(201, leaderboard_id=3)
        on.players.append(make_match_player(201, profile_id=1, new_rating=1500))
        off = make_match(202, leaderboard_id=4)
        off.players.append(make_match_player(202, profile_id=1, new_rating=1234))
        session.add_all([on, off])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        series = (await client.get("/v1/tournaments/cup/progression")).json()["items"][0]
        assert [p["rating"] for p in series["points"]] == [1500]

    async def test_scoped_to_roster(self, client: AsyncClient, session: AsyncSession):
        # Player 2 has history on the leaderboard but is not on the roster.
        for pid in (1, 2):
            p = make_player(pid, alias=f"p{pid}")
            p.ratings.append(make_player_rating(pid, leaderboard_id=3, current_rating=1500))
            session.add(p)
            match = make_match(300 + pid, leaderboard_id=3)
            match.players.append(make_match_player(300 + pid, profile_id=pid, new_rating=1500))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/progression")).json()["items"]
        assert [s["profile_id"] for s in items] == [1]

    async def test_excludes_in_progress_matches(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1, alias="hera")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=1500))
        session.add(player)
        done = make_match(401, leaderboard_id=3)
        done.players.append(make_match_player(401, profile_id=1, new_rating=1500))
        # In-progress: no completion time, no settled rating — not a point.
        live = make_match(402, leaderboard_id=3, state=MatchState.IN_PROGRESS, completed_at=None)
        live.players.append(make_match_player(402, profile_id=1, outcome=None, new_rating=None))
        session.add_all([done, live])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        series = (await client.get("/v1/tournaments/cup/progression")).json()["items"][0]
        assert [p["rating"] for p in series["points"]] == [1500]

    async def test_empty_when_no_history(self, client: AsyncClient, session: AsyncSession):
        # Rostered player with no matches → omitted; empty list overall.
        player = make_player(1, alias="hera")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=1500))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/progression")).json()
        assert body == {"last_polled_at": None, "items": []}


class TestCivStats:
    """GET /{slug}/civ-stats: civ pick/win aggregation, entrants-only (#218)."""

    async def test_overall_and_by_player_aggregation(
        self, client: AsyncClient, session: AsyncSession
    ):
        for profile_id in (1, 2):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=2000)
            )
            session.add(player)
        # profile 1: civ 27 (win, loss) + civ 13 (win); profile 2: civ 27 (loss).
        for match_id, profile_id, civ, outcome in (
            (1, 1, 27, MatchOutcome.WIN),
            (2, 1, 27, MatchOutcome.LOSS),
            (3, 1, 13, MatchOutcome.WIN),
            (4, 2, 27, MatchOutcome.LOSS),
        ):
            match = make_match(match_id, leaderboard_id=3)
            match.players.append(
                make_match_player(
                    match_id, profile_id=profile_id, civilization_id=civ, outcome=outcome
                )
            )
            session.add(match)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        session.add(tournament)
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/civ-stats")).json()
        # Overall: civ 27 picked 3× (1 win), civ 13 once (1 win). Picks desc.
        assert body["overall"] == [
            {"civilization_id": 27, "picks": 3, "wins": 1},
            {"civilization_id": 13, "picks": 1, "wins": 1},
        ]
        tp1 = tournament.tracked_players[0].id
        tp2 = tournament.tracked_players[1].id
        assert body["by_player"] == [
            {
                "tournament_player_id": tp1,
                "profile_id": 1,
                "civs": [
                    {"civilization_id": 27, "picks": 2, "wins": 1},
                    {"civilization_id": 13, "picks": 1, "wins": 1},
                ],
            },
            {
                "tournament_player_id": tp2,
                "profile_id": 2,
                "civs": [{"civilization_id": 27, "picks": 1, "wins": 0}],
            },
        ]
        assert body["last_polled_at"] is not None

    async def test_excludes_ladder_opponents(self, client: AsyncClient, session: AsyncSession):
        # The entrant's (unrostered) opponent's civ row must not be counted.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        match = make_match(1, leaderboard_id=3)
        match.players.append(
            make_match_player(
                1, profile_id=1, team_id=0, civilization_id=27, outcome=MatchOutcome.WIN
            )
        )
        match.players.append(
            make_match_player(
                1, profile_id=999, team_id=1, civilization_id=7, outcome=MatchOutcome.LOSS
            )
        )
        session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/civ-stats")).json()
        assert body["overall"] == [{"civilization_id": 27, "picks": 1, "wins": 1}]
        assert [c["civilization_id"] for c in body["by_player"][0]["civs"]] == [27]

    async def test_windowed_to_tournament_dates(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # day 1 pre-window, day 10 in-window, day 20 post-window.
        for match_id, day, civ in ((1, 1, 27), (2, 10, 13), (3, 20, 99)):
            match = make_match(
                match_id, leaderboard_id=3, started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC)
            )
            match.players.append(
                make_match_player(
                    match_id, profile_id=1, civilization_id=civ, outcome=MatchOutcome.WIN
                )
            )
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1],
                leaderboard_id=3,
                start_date=datetime(2026, 5, 5, tzinfo=UTC),
                grand_finals_date=datetime(2026, 5, 15, tzinfo=UTC),
            )
        )
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/civ-stats")).json()
        assert body["overall"] == [{"civilization_id": 13, "picks": 1, "wins": 1}]

    async def test_scoped_to_tournament_leaderboard(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        on = make_match(1, leaderboard_id=3)
        on.players.append(
            make_match_player(1, profile_id=1, civilization_id=27, outcome=MatchOutcome.WIN)
        )
        off = make_match(2, leaderboard_id=4)
        off.players.append(
            make_match_player(2, profile_id=1, civilization_id=13, outcome=MatchOutcome.WIN)
        )
        session.add_all([on, off])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/civ-stats")).json()
        assert body["overall"] == [{"civilization_id": 27, "picks": 1, "wins": 1}]

    async def test_excludes_in_progress_matches(self, client: AsyncClient, session: AsyncSession):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        done = make_match(1, leaderboard_id=3)
        done.players.append(
            make_match_player(1, profile_id=1, civilization_id=27, outcome=MatchOutcome.WIN)
        )
        live = make_match(2, leaderboard_id=3, state=MatchState.IN_PROGRESS, completed_at=None)
        live.players.append(
            make_match_player(2, profile_id=1, civilization_id=13, outcome=None, new_rating=None)
        )
        session.add_all([done, live])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/civ-stats")).json()
        assert body["overall"] == [{"civilization_id": 27, "picks": 1, "wins": 1}]

    async def test_empty_when_roster_has_no_matches(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/civ-stats")).json()
        assert body == {"last_polled_at": None, "overall": [], "by_player": []}

    async def test_empty_when_no_roster(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/civ-stats")).json()
        assert body == {"last_polled_at": None, "overall": [], "by_player": []}

    async def test_cache_control_header_unauthenticated(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Viewer path opts into the shared CDN cache, like the other polled reads.
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/civ-stats")
        assert (
            response.headers["Cache-Control"] == "public, s-maxage=15, max-age=0, must-revalidate"
        )


class TestStandingsStreamLive:
    """stream_live: folded onto the standings row from the live_streams snapshot."""

    async def test_reflects_live_streams_table(self, client: AsyncClient, session: AsyncSession):
        for profile_id in (1, 2):
            player = make_player(profile_id, alias=f"p{profile_id}")
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=2000 - profile_id)
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        session.add(tournament)
        await session.flush()
        # Roster row for profile 1 is live (on any platform); row 2 is not.
        row1 = next(p for p in tournament.tracked_players if p.profile_id == 1)
        session.add(LiveStream(tournament_player_id=row1.id, platform="twitch"))
        await session.commit()

        rows = {
            row["profile_id"]: row
            for row in (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        }
        assert rows[1]["stream_live"] is True
        assert rows[2]["stream_live"] is False

    async def test_placeholder_row_can_be_live(self, client: AsyncClient, session: AsyncSession):
        """#147: a placeholder row (profile_id IS NULL) reports stream_live too.

        The broadcast-live snapshot is keyed on TournamentPlayer.id, so a
        placeholder's surrogate row id is a first-class participant.
        """
        tournament = make_tournament("cup")
        placeholder = TournamentPlayer(
            name="iyouxin", presentation={"streamUrls": ["https://twitch.tv/iyouxin"]}
        )
        tournament.tracked_players = [placeholder]
        session.add(tournament)
        await session.flush()
        session.add(LiveStream(tournament_player_id=placeholder.id, platform="twitch"))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert len(items) == 1
        assert items[0]["profile_id"] is None
        assert items[0]["alias"] == "iyouxin"
        assert items[0]["stream_live"] is True
