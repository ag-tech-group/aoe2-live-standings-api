"""GET /v1/tournaments, /v1/tournaments/{slug}, and /{slug}/standings."""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Civilization,
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
from app.routers.tournaments import (
    _RECENT_RESULTS_LIMIT,
    _civilization_names,
    _reset_civilization_names_cache,
)
from tests.conftest import (
    DEFAULT_TEST_USER_ID,
    make_match,
    make_match_player,
    make_player,
    make_player_rating,
    make_team,
    make_tournament,
)


def _day(day: int) -> datetime:
    """Noon UTC on the given day of May 2026 — a distinct, ordered match start."""
    return datetime(2026, 5, day, 12, 0, tzinfo=UTC)


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
        # Peaks all tie at the factory default, so current_rating breaks the tie.
        assert [row["alias"] for row in items] == ["high", "mid", "low"]
        assert [row["current_rating"] for row in items] == [2500, 2000, 1500]

    async def test_sorted_by_peak_then_current(self, client: AsyncClient, session: AsyncSession):
        # Position is by peak (max_rating); current_rating only breaks peak
        # ties (#226). A leads on peak despite the lowest current; B beats C on
        # current within their equal peak.
        for profile_id, alias, current, peak in (
            (1, "A", 1500, 2500),
            (2, "B", 2400, 2000),
            (3, "C", 1800, 2000),
        ):
            player = make_player(profile_id, alias=alias)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=current, max_rating=peak
                )
            )
            session.add(player)
        session.add(make_tournament("cup", profile_ids=[1, 2, 3], leaderboard_id=3))
        await session.commit()

        items = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        assert [row["alias"] for row in items] == ["A", "B", "C"]
        assert [row["max_rating"] for row in items] == [2500, 2000, 2000]
        assert [row["current_rating"] for row in items] == [1500, 2400, 1800]

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

    async def test_name_resolves_display_name_override(
        self, client: AsyncClient, session: AsyncSession
    ):
        # `name` returns the host's presentation.displayName override; the raw
        # polled ladder handle stays in `alias` (#243). A row with no override
        # falls back to the base roster name.
        for profile_id, rating in ((1, 2500), (2, 2400)):
            player = make_player(profile_id, alias=f"handle{profile_id}")
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, current_rating=rating)
            )
            session.add(player)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        for tracked in tournament.tracked_players:
            if tracked.profile_id == 1:
                tracked.name = "pigrandom"
                tracked.presentation = {"displayName": "PiG"}
            else:
                tracked.name = "plain"
        session.add(tournament)
        await session.commit()

        rows = {
            row["profile_id"]: row
            for row in (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        }
        assert rows[1]["name"] == "PiG"
        assert rows[1]["alias"] == "handle1"
        assert rows[2]["name"] == "plain"  # no override → base roster name
        assert rows[2]["alias"] == "handle2"


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
            "longest_win_streak": 0,
            "peak_rating": None,
            "last_match_at": None,
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
            "longest_win_streak": 0,
            "peak_rating": None,
            "last_match_at": None,
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

    async def test_longest_win_streak_is_the_peak_not_the_current_run(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # Oldest -> newest: WIN, WIN, WIN, LOSS, WIN. Current streak is just
        # the trailing +1; the longest win run earlier in the window is 3.
        for match_id, day, outcome in (
            (1, 1, MatchOutcome.WIN),
            (2, 2, MatchOutcome.WIN),
            (3, 3, MatchOutcome.WIN),
            (4, 4, MatchOutcome.LOSS),
            (5, 5, MatchOutcome.WIN),
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

        record = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0][
            "tournament_record"
        ]
        assert record["streak"] == 1
        assert record["longest_win_streak"] == 3

    async def test_longest_win_streak_is_zero_without_wins(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        for match_id in (1, 2):
            match = make_match(match_id, leaderboard_id=3)
            match.players.append(
                make_match_player(match_id, profile_id=1, outcome=MatchOutcome.LOSS)
            )
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        record = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0][
            "tournament_record"
        ]
        assert record["longest_win_streak"] == 0

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
            "longest_win_streak": 0,
            "peak_rating": None,
            "last_match_at": None,
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

    async def test_folds_in_civ_names(self, client: AsyncClient, session: AsyncSession):
        # Both the entrant's and opponent's civ names are folded in (#227).
        session.add_all(
            [
                Civilization(civilization_id=7, name="Burgundians"),
                Civilization(civilization_id=23, name="Japanese"),
            ]
        )
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        match = make_match(1, leaderboard_id=3)
        match.players.append(
            make_match_player(
                1, profile_id=1, team_id=0, civilization_id=7, outcome=MatchOutcome.WIN
            )
        )
        match.players.append(make_match_player(1, profile_id=999, team_id=1, civilization_id=23))
        session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        matchup = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0][
            "tournament_record"
        ]["recent_matchups"][0]
        assert matchup["civilization_name"] == "Burgundians"
        assert matchup["opponent_civilization_name"] == "Japanese"

    async def test_capped_window_scoped_and_newest_first(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Newest-first, capped at the server limit, and windowed to the
        # tournament dates: out-of-window games are dropped on both bounds —
        # even a post-window game newer than every counted one.
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        # 12 in-window games (civ id == match id == day) bracketed by one
        # pre-window and one post-window game on the same leaderboard.
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
        pre.players.append(
            make_match_player(
                99, profile_id=1, team_id=0, civilization_id=5, outcome=MatchOutcome.WIN
            )
        )
        session.add(pre)
        # Newer than every in-window game, but past grand_finals → still dropped.
        post = make_match(98, leaderboard_id=3, started_at=datetime(2027, 1, 1, 12, 0, tzinfo=UTC))
        post.players.append(
            make_match_player(
                98, profile_id=1, team_id=0, civilization_id=98, outcome=MatchOutcome.WIN
            )
        )
        session.add(post)
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
        matchups = record["recent_matchups"]
        # Capped at the limit; both the pre- (99) and post-window (98) games dropped.
        assert len(matchups) == _RECENT_RESULTS_LIMIT
        # civ id == match id here; newest-first = matches 12..3.
        assert [m["civilization_id"] for m in matchups] == list(range(12, 2, -1))
        assert [m["outcome"] for m in matchups] == [
            "win" if mid % 2 == 0 else "loss" for mid in range(12, 2, -1)
        ]


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
            {"civilization_id": 27, "name": None, "picks": 3, "wins": 1},
            {"civilization_id": 13, "name": None, "picks": 1, "wins": 1},
        ]
        tp1 = tournament.tracked_players[0].id
        tp2 = tournament.tracked_players[1].id
        assert body["by_player"] == [
            {
                "tournament_player_id": tp1,
                "profile_id": 1,
                "civs": [
                    {"civilization_id": 27, "name": None, "picks": 2, "wins": 1},
                    {"civilization_id": 13, "name": None, "picks": 1, "wins": 1},
                ],
            },
            {
                "tournament_player_id": tp2,
                "profile_id": 2,
                "civs": [{"civilization_id": 27, "name": None, "picks": 1, "wins": 0}],
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
        assert body["overall"] == [{"civilization_id": 27, "name": None, "picks": 1, "wins": 1}]
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
        assert body["overall"] == [{"civilization_id": 13, "name": None, "picks": 1, "wins": 1}]

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
        assert body["overall"] == [{"civilization_id": 27, "name": None, "picks": 1, "wins": 1}]

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
        assert body["overall"] == [{"civilization_id": 27, "name": None, "picks": 1, "wins": 1}]

    async def test_includes_armenians_civ_zero_excludes_sentinel(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Civ id 0 is Armenians, a real civ — it must be counted. Only the
        # missing-civ sentinel (-1) is skipped (#227 corrected the earlier
        # mistake of excluding civ 0).
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        armenians = make_match(1, leaderboard_id=3)
        armenians.players.append(
            make_match_player(1, profile_id=1, civilization_id=0, outcome=MatchOutcome.WIN)
        )
        unknown = make_match(2, leaderboard_id=3)
        unknown.players.append(
            make_match_player(2, profile_id=1, civilization_id=-1, outcome=MatchOutcome.WIN)
        )
        session.add_all([armenians, unknown])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/civ-stats")).json()
        # Civ 0 (Armenians) is counted; the -1 sentinel is omitted.
        assert body["overall"] == [{"civilization_id": 0, "name": None, "picks": 1, "wins": 1}]
        assert [c["civilization_id"] for c in body["by_player"][0]["civs"]] == [0]

    async def test_folds_in_civilization_name(self, client: AsyncClient, session: AsyncSession):
        # The civ name is folded from the civilizations reference (#227).
        session.add(Civilization(civilization_id=7, name="Burgundians"))
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        match = make_match(1, leaderboard_id=3)
        match.players.append(
            make_match_player(1, profile_id=1, civilization_id=7, outcome=MatchOutcome.WIN)
        )
        session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/civ-stats")).json()
        assert body["overall"] == [
            {"civilization_id": 7, "name": "Burgundians", "picks": 1, "wins": 1}
        ]
        assert body["by_player"][0]["civs"][0]["name"] == "Burgundians"

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


class TestTournamentSummary:
    """GET /{slug}/summary: headline "leader" stat cards (#238)."""

    _ALL_NULL = {
        "last_polled_at": None,
        "highest_peak_rating": None,
        "best_win_rate": None,
        "longest_win_streak": None,
        "biggest_climber": None,
        "most_games_played": None,
    }

    async def test_empty_roster_returns_all_null_cards(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup", leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        assert body == self._ALL_NULL

    async def test_rated_entrant_without_matches_leads_only_the_peak_card(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A rostered, rated entrant with no in-window matches earns nothing on
        # the four in-window cards — but the lifetime peak card reads all-time
        # max_rating, so it still names them (#246 corrects #244). The other
        # four stay null rather than a value-0 card naming an arbitrary player.
        player = make_player(1)
        player.ratings.append(
            make_player_rating(1, leaderboard_id=3, current_rating=2000, max_rating=2000)
        )
        session.add(player)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        session.add(tournament)
        await session.commit()
        tp = {p.profile_id: p.id for p in tournament.tracked_players}

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        assert body["highest_peak_rating"] == {
            "tournament_player_id": tp[1],
            "profile_id": 1,
            "name": "p1",
            "value": 2000,
        }
        assert body["best_win_rate"] is None
        assert body["longest_win_streak"] is None
        assert body["biggest_climber"] is None
        assert body["most_games_played"] is None

    async def test_picks_the_leader_for_each_card(self, client: AsyncClient, session: AsyncSession):
        # Three entrants, each dominant on a different metric:
        #   p1 — four straight wins (streak leader), rating 1510→1540 (+30
        #        climber leader), only 4 games
        #   p2 — one peak at 2100 (rating leader), rating 2100→2050 (−50), 2 games
        #   p3 — eight games, six wins (games + win-rate leader), flat at 1600 (0)
        # All-time max_rating tracks each one's in-window peak here, so p2 leads
        # the lifetime peak card too; the all-time-vs-in-window divergence has
        # its own dedicated test.
        for profile_id, max_rating in ((1, 1540), (2, 2100), (3, 1600)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(profile_id, leaderboard_id=3, max_rating=max_rating)
            )
            session.add(player)
        # p1: WIN x4, peak new_rating 1540.
        for match_id, day, rating in ((1, 1, 1510), (2, 2, 1520), (3, 3, 1530), (4, 4, 1540)):
            match = make_match(match_id, leaderboard_id=3, started_at=_day(day))
            match.players.append(
                make_match_player(
                    match_id, profile_id=1, outcome=MatchOutcome.WIN, new_rating=rating
                )
            )
            session.add(match)
        # p2: WIN (rating 2100) then LOSS (rating 2050) — peak 2100, 1 win.
        for match_id, day, outcome, rating in (
            (5, 1, MatchOutcome.WIN, 2100),
            (6, 2, MatchOutcome.LOSS, 2050),
        ):
            match = make_match(match_id, leaderboard_id=3, started_at=_day(day))
            match.players.append(
                make_match_player(match_id, profile_id=2, outcome=outcome, new_rating=rating)
            )
            session.add(match)
        # p3: W W W L W W L W over days 1-8 — 6 wins / 8 games, longest run 3.
        p3 = (
            MatchOutcome.WIN,
            MatchOutcome.WIN,
            MatchOutcome.WIN,
            MatchOutcome.LOSS,
            MatchOutcome.WIN,
            MatchOutcome.WIN,
            MatchOutcome.LOSS,
            MatchOutcome.WIN,
        )
        for offset, outcome in enumerate(p3):
            match_id = 7 + offset
            match = make_match(match_id, leaderboard_id=3, started_at=_day(1 + offset))
            match.players.append(
                make_match_player(match_id, profile_id=3, outcome=outcome, new_rating=1600)
            )
            session.add(match)
        tournament = make_tournament("cup", profile_ids=[1, 2, 3], leaderboard_id=3)
        session.add(tournament)
        await session.commit()
        tp = {p.profile_id: p.id for p in tournament.tracked_players}

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        streak = body["longest_win_streak"]
        assert {k: streak[k] for k in ("tournament_player_id", "profile_id", "name", "value")} == {
            "tournament_player_id": tp[1],
            "profile_id": 1,
            "name": "p1",
            "value": 4,
        }
        # The streak card always carries the date keys; exact values (which
        # need per-match completion times) are covered in the dedicated test.
        assert "streak_start" in streak and "streak_end" in streak
        assert body["highest_peak_rating"] == {
            "tournament_player_id": tp[2],
            "profile_id": 2,
            "name": "p2",
            "value": 2100,
        }
        assert body["most_games_played"] == {
            "tournament_player_id": tp[3],
            "profile_id": 3,
            "name": "p3",
            "value": 8,
        }
        # p1 climbed +30 in-window (1510→1540); p2 fell −50, p3 was flat.
        assert body["biggest_climber"] == {
            "tournament_player_id": tp[1],
            "profile_id": 1,
            "name": "p1",
            "value": 30,
        }
        # p3 (75% over 8) leads on win rate; p1's 100% over 4 games is below
        # the min-games guard, so it does not headline.
        assert body["best_win_rate"] == {
            "tournament_player_id": tp[3],
            "profile_id": 3,
            "name": "p3",
            "value": 75.0,
        }
        assert body["last_polled_at"] is not None

    async def test_longest_win_streak_card_carries_peak_run_dates(
        self, client: AsyncClient, session: AsyncSession
    ):
        # The peak run is the first five games (days 1-5); the next ten games
        # alternate so their longest run is 1. recent_matchups caps at the
        # last 10 (days 6-15), so the streak's dates can only come from the
        # full-history walk — exactly the gap this card fills.
        session.add(make_player(1))
        outcomes = [MatchOutcome.WIN] * 5 + [MatchOutcome.LOSS, MatchOutcome.WIN] * 5
        for offset, outcome in enumerate(outcomes):
            day = 1 + offset
            match = make_match(
                offset + 1,
                leaderboard_id=3,
                started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC),
                completed_at=datetime(2026, 5, day, 12, 30, tzinfo=UTC),
            )
            match.players.append(make_match_player(offset + 1, profile_id=1, outcome=outcome))
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        card = (await client.get("/v1/tournaments/cup/summary")).json()["longest_win_streak"]
        assert card["value"] == 5
        # start = first (oldest) win's completion; end = last (newest) win's.
        assert card["streak_start"].startswith("2026-05-01T12:30:00")
        assert card["streak_end"].startswith("2026-05-05T12:30:00")

    async def test_best_win_rate_guard_excludes_small_samples(
        self, client: AsyncClient, session: AsyncSession
    ):
        # p1 is 1-0 (100%, 1 game); p2 is 4-1 (80%, 5 games). The guard keeps
        # p1's perfect-but-tiny sample from headlining over p2's real one.
        for profile_id in (1, 2):
            session.add(make_player(profile_id))
        match = make_match(1, leaderboard_id=3, started_at=_day(1))
        match.players.append(make_match_player(1, profile_id=1, outcome=MatchOutcome.WIN))
        session.add(match)
        p2 = (
            MatchOutcome.WIN,
            MatchOutcome.WIN,
            MatchOutcome.WIN,
            MatchOutcome.WIN,
            MatchOutcome.LOSS,
        )
        for offset, outcome in enumerate(p2):
            match_id = 2 + offset
            m = make_match(match_id, leaderboard_id=3, started_at=_day(1 + offset))
            m.players.append(make_match_player(match_id, profile_id=2, outcome=outcome))
            session.add(m)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        session.add(tournament)
        await session.commit()
        tp = {p.profile_id: p.id for p in tournament.tracked_players}

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        assert body["best_win_rate"]["profile_id"] == 2
        assert body["best_win_rate"]["value"] == 80.0
        assert body["best_win_rate"]["tournament_player_id"] == tp[2]

    async def test_best_win_rate_null_when_no_one_meets_min_games(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A lone 1-0 player: a real most-games leader, but no win-rate leader
        # (nobody clears the min-games guard) and no climber (one rated point
        # isn't enough to measure a change across).
        session.add(make_player(1))
        match = make_match(1, leaderboard_id=3)
        match.players.append(make_match_player(1, profile_id=1, outcome=MatchOutcome.WIN))
        session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        assert body["best_win_rate"] is None
        assert body["biggest_climber"] is None
        assert body["most_games_played"]["value"] == 1

    async def test_win_rate_min_games_query_param_overrides_default(
        self, client: AsyncClient, session: AsyncSession
    ):
        # p1 is 3-0: a perfect record below the default guard of 5 games.
        session.add(make_player(1))
        for offset in range(3):
            match_id = 1 + offset
            m = make_match(match_id, leaderboard_id=3, started_at=_day(1 + offset))
            m.players.append(make_match_player(match_id, profile_id=1, outcome=MatchOutcome.WIN))
            session.add(m)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        # Default guard (5): the 3-game sample is too small to headline.
        assert (await client.get("/v1/tournaments/cup/summary")).json()["best_win_rate"] is None
        # Lowering the guard to 3 lets it qualify; the other cards are unchanged.
        body = (await client.get("/v1/tournaments/cup/summary?win_rate_min_games=3")).json()
        assert body["best_win_rate"]["profile_id"] == 1
        assert body["best_win_rate"]["value"] == 100.0
        assert body["most_games_played"]["value"] == 3

    async def test_win_rate_min_games_rejects_below_one(
        self, client: AsyncClient, session: AsyncSession
    ):
        # The guard needs at least one game to be a meaningful win rate.
        session.add(make_tournament("cup", leaderboard_id=3))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/summary?win_rate_min_games=0")
        assert response.status_code == 422

    async def test_cards_null_when_metric_unearned(
        self, client: AsyncClient, session: AsyncSession
    ):
        # An all-losses entrant: real games and a peak rating, but zero wins —
        # so longest_win_streak is null while most_games_played and
        # highest_peak_rating still name them. (Both losses settle at 1480, so
        # the climber is a flat 0 — a value, not null; see the climber tests.)
        player = make_player(1)
        player.ratings.append(make_player_rating(1, leaderboard_id=3, max_rating=1480))
        session.add(player)
        for match_id in (1, 2):
            match = make_match(match_id, leaderboard_id=3, started_at=_day(match_id))
            match.players.append(
                make_match_player(
                    match_id, profile_id=1, outcome=MatchOutcome.LOSS, new_rating=1480
                )
            )
            session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        assert body["longest_win_streak"] is None
        assert body["most_games_played"]["value"] == 2
        assert body["highest_peak_rating"]["value"] == 1480

    async def test_highest_peak_rating_ranks_by_all_time_max_not_in_window_peak(
        self, client: AsyncClient, session: AsyncSession
    ):
        # The peak card reads the immutable all-time max_rating (the host's
        # decision), NOT the in-window tournament_record.peak_rating. p1's
        # all-time max (1764) is highest even though its in-window peak (1694)
        # trails p2's in-window peak (1702); p2's all-time max is only 1739. So
        # p1 leads. (#246 corrects #244, which ranked by the in-window peak and
        # would name p2/1702 here.)
        p1 = make_player(1)
        p1.ratings.append(
            make_player_rating(1, leaderboard_id=3, current_rating=1694, max_rating=1764)
        )
        p2 = make_player(2)
        p2.ratings.append(
            make_player_rating(2, leaderboard_id=3, current_rating=1702, max_rating=1739)
        )
        session.add_all([p1, p2])
        # In-window matches: p1 peaks at 1694, p2 at 1702 — the reverse of the
        # all-time order, so ranking by the wrong column flips the leader.
        m1 = make_match(1, leaderboard_id=3, started_at=_day(1))
        m1.players.append(make_match_player(1, profile_id=1, new_rating=1694))
        m2 = make_match(2, leaderboard_id=3, started_at=_day(1))
        m2.players.append(make_match_player(2, profile_id=2, new_rating=1702))
        session.add_all([m1, m2])
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        session.add(tournament)
        await session.commit()
        tp = {p.profile_id: p.id for p in tournament.tracked_players}

        card = (await client.get("/v1/tournaments/cup/summary")).json()["highest_peak_rating"]
        assert card == {
            "tournament_player_id": tp[1],
            "profile_id": 1,
            "name": "p1",
            "value": 1764,
        }

    async def test_highest_peak_rating_value_matches_standings_max_rating(
        self, client: AsyncClient, session: AsyncSession
    ):
        # The peak card's value is exactly the all-time max_rating /standings
        # exposes as StandingRow.max_rating (the PEAK column) — one source of
        # truth, distinct from the in-window peak (here 1820 vs 1600).
        player = make_player(1)
        player.ratings.append(
            make_player_rating(1, leaderboard_id=3, current_rating=1600, max_rating=1820)
        )
        session.add(player)
        match = make_match(1, leaderboard_id=3, started_at=_day(1))
        match.players.append(make_match_player(1, profile_id=1, new_rating=1600))
        session.add(match)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        summary = (await client.get("/v1/tournaments/cup/summary")).json()
        standings = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        standings_max = {row["profile_id"]: row["max_rating"] for row in standings}[1]
        assert summary["highest_peak_rating"]["value"] == standings_max == 1820

    async def test_biggest_climber_is_signed_and_can_be_negative(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Everyone's rating fell in-window; the climber card still names a
        # leader — the one who dropped the least — with a negative value.
        for profile_id in (1, 2):
            session.add(make_player(profile_id))
        # p1: 1480 → 1400 (−80).
        for match_id, day, rating in ((1, 1, 1480), (2, 2, 1400)):
            m = make_match(match_id, leaderboard_id=3, started_at=_day(day))
            m.players.append(
                make_match_player(
                    match_id, profile_id=1, outcome=MatchOutcome.LOSS, new_rating=rating
                )
            )
            session.add(m)
        # p2: 1490 → 1470 (−20) — the least-dropped, so it leads.
        for match_id, day, rating in ((3, 1, 1490), (4, 2, 1470)):
            m = make_match(match_id, leaderboard_id=3, started_at=_day(day))
            m.players.append(
                make_match_player(
                    match_id, profile_id=2, outcome=MatchOutcome.LOSS, new_rating=rating
                )
            )
            session.add(m)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        session.add(tournament)
        await session.commit()
        tp = {p.profile_id: p.id for p in tournament.tracked_players}

        card = (await client.get("/v1/tournaments/cup/summary")).json()["biggest_climber"]
        assert card == {
            "tournament_player_id": tp[2],
            "profile_id": 2,
            "name": "p2",
            "value": -20,
        }

    async def test_biggest_climber_window_scoped(self, client: AsyncClient, session: AsyncSession):
        # Pre/post-window points are excluded: the delta is the last minus the
        # first *in-window* rated point (1600−1500=100), not the lifetime
        # 3000−1000 the FE's full /progression series would have measured.
        session.add(make_player(1))
        for match_id, day, rating in (
            (1, 1, 1000),  # pre-window
            (2, 10, 1500),  # first in-window rated point
            (3, 11, 1600),  # last in-window rated point
            (4, 20, 3000),  # post-window
        ):
            m = make_match(
                match_id, leaderboard_id=3, started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC)
            )
            m.players.append(make_match_player(match_id, profile_id=1, new_rating=rating))
            session.add(m)
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

        card = (await client.get("/v1/tournaments/cup/summary")).json()["biggest_climber"]
        assert card["profile_id"] == 1
        assert card["value"] == 100

    async def test_biggest_climber_null_when_under_two_rated_points(
        self, client: AsyncClient, session: AsyncSession
    ):
        # One rated in-window match plus an unranked one (null new_rating, which
        # doesn't count) — fewer than two rated points, nothing to measure a
        # change across, so the card is null. A metric that needs no rating
        # delta (games played) still names the entrant.
        session.add(make_player(1))
        rated = make_match(1, leaderboard_id=3, started_at=_day(1))
        rated.players.append(make_match_player(1, profile_id=1, new_rating=1500))
        unranked = make_match(2, leaderboard_id=3, started_at=_day(2))
        unranked.players.append(make_match_player(2, profile_id=1, new_rating=None))
        session.add_all([rated, unranked])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        assert body["biggest_climber"] is None
        assert body["most_games_played"]["value"] == 2

    async def test_biggest_climber_tie_break_prefers_more_games(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Both climb +50 in-window; p2 played more games, so it takes the tie —
        # the same total order (games desc, then id asc) as the other cards.
        for profile_id in (1, 2):
            session.add(make_player(profile_id))
        # p1: 1500 → 1550 over 2 games (+50).
        for match_id, day, rating in ((1, 1, 1500), (2, 2, 1550)):
            m = make_match(match_id, leaderboard_id=3, started_at=_day(day))
            m.players.append(make_match_player(match_id, profile_id=1, new_rating=rating))
            session.add(m)
        # p2: 1500 → 1550 over 3 games (+50) — more games breaks the tie.
        for match_id, day, rating in ((3, 1, 1500), (4, 2, 1520), (5, 3, 1550)):
            m = make_match(match_id, leaderboard_id=3, started_at=_day(day))
            m.players.append(make_match_player(match_id, profile_id=2, new_rating=rating))
            session.add(m)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        session.add(tournament)
        await session.commit()
        tp = {p.profile_id: p.id for p in tournament.tracked_players}

        card = (await client.get("/v1/tournaments/cup/summary")).json()["biggest_climber"]
        assert card["value"] == 50
        assert card["profile_id"] == 2
        assert card["tournament_player_id"] == tp[2]

    async def test_card_name_resolves_display_name_override(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A card's `name` is the resolved display label — the host's
        # presentation.displayName override, not the base roster handle (#243).
        session.add(make_player(1))
        for match_id in (1, 2):
            match = make_match(match_id, leaderboard_id=3, started_at=_day(match_id))
            match.players.append(
                make_match_player(match_id, profile_id=1, outcome=MatchOutcome.WIN)
            )
            session.add(match)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        tournament.tracked_players[0].name = "pigrandom"
        tournament.tracked_players[0].presentation = {"displayName": "PiG"}
        session.add(tournament)
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        assert body["most_games_played"]["name"] == "PiG"
        assert body["longest_win_streak"]["name"] == "PiG"

    async def test_tie_break_prefers_more_games(self, client: AsyncClient, session: AsyncSession):
        # Both entrants peak at a 3-win streak; p2 played more games, so it
        # wins the tie (the streak is otherwise equal).
        for profile_id in (1, 2):
            session.add(make_player(profile_id))
        for offset in range(3):  # p1: W W W (3 games)
            match_id = 1 + offset
            m = make_match(match_id, leaderboard_id=3, started_at=_day(1 + offset))
            m.players.append(make_match_player(match_id, profile_id=1, outcome=MatchOutcome.WIN))
            session.add(m)
        p2 = (
            MatchOutcome.WIN,
            MatchOutcome.WIN,
            MatchOutcome.WIN,
            MatchOutcome.LOSS,
            MatchOutcome.LOSS,
        )  # longest run still 3, but 5 games
        for offset, outcome in enumerate(p2):
            match_id = 4 + offset
            m = make_match(match_id, leaderboard_id=3, started_at=_day(1 + offset))
            m.players.append(make_match_player(match_id, profile_id=2, outcome=outcome))
            session.add(m)
        session.add(make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3))
        await session.commit()

        card = (await client.get("/v1/tournaments/cup/summary")).json()["longest_win_streak"]
        assert card["value"] == 3
        assert card["profile_id"] == 2

    async def test_tie_break_prefers_lower_tournament_player_id(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Fully tied (3-win streak, 3 games each) — the lower roster id wins.
        for profile_id in (1, 2):
            session.add(make_player(profile_id))
        for profile_id, base in ((1, 1), (2, 4)):
            for offset in range(3):
                match_id = base + offset
                m = make_match(match_id, leaderboard_id=3, started_at=_day(1 + offset))
                m.players.append(
                    make_match_player(match_id, profile_id=profile_id, outcome=MatchOutcome.WIN)
                )
                session.add(m)
        tournament = make_tournament("cup", profile_ids=[1, 2], leaderboard_id=3)
        session.add(tournament)
        await session.commit()
        tp = {p.profile_id: p.id for p in tournament.tracked_players}

        card = (await client.get("/v1/tournaments/cup/summary")).json()["longest_win_streak"]
        assert card["tournament_player_id"] == min(tp[1], tp[2])
        assert card["profile_id"] == 1

    async def test_windowed_to_tournament_dates(self, client: AsyncClient, session: AsyncSession):
        # Wins on day 1 (pre), day 10 (in), day 20 (post); only day 10 counts.
        session.add(make_player(1))
        for match_id, day in ((1, 1), (2, 10), (3, 20)):
            match = make_match(
                match_id, leaderboard_id=3, started_at=datetime(2026, 5, day, 12, 0, tzinfo=UTC)
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
                grand_finals_date=datetime(2026, 5, 15, tzinfo=UTC),
            )
        )
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        assert body["most_games_played"]["value"] == 1
        assert body["longest_win_streak"]["value"] == 1

    async def test_only_linked_entrants_considered(
        self, client: AsyncClient, session: AsyncSession
    ):
        # An unlinked roster row (no profile_id) has no match data and can win
        # no card; the linked entrant leads and the response is well-formed.
        session.add(make_player(1))
        for match_id in (1, 2):
            match = make_match(match_id, leaderboard_id=3, started_at=_day(match_id))
            match.players.append(
                make_match_player(match_id, profile_id=1, outcome=MatchOutcome.WIN)
            )
            session.add(match)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        tournament.tracked_players.append(TournamentPlayer(name="ghost"))
        session.add(tournament)
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        assert body["most_games_played"]["profile_id"] == 1
        assert body["most_games_played"]["value"] == 2

    async def test_scoped_to_tournament_leaderboard(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A win on another leaderboard must not count toward the cards.
        session.add(make_player(1))
        on = make_match(1, leaderboard_id=3, started_at=_day(1))
        on.players.append(make_match_player(1, profile_id=1, outcome=MatchOutcome.WIN))
        off = make_match(2, leaderboard_id=4, started_at=_day(2))
        off.players.append(make_match_player(2, profile_id=1, outcome=MatchOutcome.WIN))
        session.add_all([on, off])
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/summary")).json()
        assert body["most_games_played"]["value"] == 1

    async def test_cache_control_header_unauthenticated(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Viewer path opts into the shared CDN cache, like the other polled reads.
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/summary")
        assert (
            response.headers["Cache-Control"] == "public, s-maxage=15, max-age=0, must-revalidate"
        )


class TestStandingsHistory:
    """GET /{slug}/standings/history: position + combined-elo over daily buckets (#219)."""

    async def test_player_position_series_by_peak(self, client: AsyncClient, session: AsyncSession):
        # P1 plays days 1-3; P2 days 2-3. Daily buckets; position by peak-so-far.
        # Polled identities — a linked row needs a Player row to pass the same
        # /standings visibility gate (#232).
        session.add(make_player(1))
        session.add(make_player(2))
        for match_id, profile_id, day, rating in (
            (1, 1, 1, 1500),
            (2, 1, 2, 1520),
            (3, 1, 3, 1510),  # P1 peak stays 1520
            (4, 2, 2, 1530),
            (5, 2, 3, 1540),
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 6, day, 12, 0, tzinfo=UTC),
                completed_at=datetime(2026, 6, day, 12, 30, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(match_id, profile_id=profile_id, new_rating=rating)
            )
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1, 2],
                leaderboard_id=3,
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
                grand_finals_date=datetime(2026, 6, 30, tzinfo=UTC),
            )
        )
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/standings/history")).json()
        by_profile = {p["profile_id"]: p for p in body["players"]}
        n = len(body["buckets"])
        # Daily anchors + a marker at each shift; everyone holds a position at
        # every bucket (no nulls), #226.
        assert n >= 3
        assert all(len(p["points"]) == n for p in body["players"])
        # P1 leads the opening bucket (P2 hasn't played); by the end P2's peak
        # (1540) overtakes P1's (1520) — a real shift captured in between.
        assert by_profile[1]["points"][0]["position"] == 1
        assert by_profile[1]["points"][-1] == {"position": 2, "peak_rating": 1520}
        assert by_profile[2]["points"][-1] == {"position": 1, "peak_rating": 1540}
        assert body["last_polled_at"] is not None
        assert body["teams"] == []
        # Each series is self-describing — it carries its display name, so the
        # FE legend needs no join back to /standings.
        assert by_profile[1]["name"] == "p1"
        assert by_profile[2]["name"] == "p2"

    async def test_series_name_resolves_display_name_override(
        self, client: AsyncClient, session: AsyncSession
    ):
        # players[].name is the resolved display label (presentation.displayName
        # override, #243), so the chart legend matches the live table with no
        # join; a row with no override keeps its base roster name.
        session.add(make_player(1))
        session.add(make_player(2))
        for match_id, profile_id, rating in ((1, 1, 1500), (2, 2, 1400)):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                completed_at=datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(match_id, profile_id=profile_id, new_rating=rating)
            )
            session.add(match)
        tournament = make_tournament(
            "cup",
            profile_ids=[1, 2],
            leaderboard_id=3,
            start_date=datetime(2026, 6, 1, tzinfo=UTC),
        )
        for tracked in tournament.tracked_players:
            if tracked.profile_id == 1:
                tracked.name = "pigrandom"
                tracked.presentation = {"displayName": "PiG"}
        session.add(tournament)
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/standings/history")).json()
        by_profile = {p["profile_id"]: p for p in body["players"]}
        assert by_profile[1]["name"] == "PiG"
        assert by_profile[2]["name"] == "p2"

    async def test_position_by_peak_not_current_rating(
        self, client: AsyncClient, session: AsyncSession
    ):
        # P1 peaks at 1600 then drops to 1400; P2 steady 1500. On day 2 P1's
        # current (1400) < P2's (1500), but P1's peak (1600) > P2's (1500), so
        # P1 still outranks P2 — position is by peak, not current.
        session.add(make_player(1))
        session.add(make_player(2))
        for match_id, profile_id, day, rating in (
            (1, 1, 1, 1600),
            (2, 1, 2, 1400),
            (3, 2, 2, 1500),
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 6, day, 12, 0, tzinfo=UTC),
                completed_at=datetime(2026, 6, day, 12, 30, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(match_id, profile_id=profile_id, new_rating=rating)
            )
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1, 2],
                leaderboard_id=3,
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/standings/history")).json()
        by_profile = {p["profile_id"]: p for p in body["players"]}
        # P1 stays ahead on peak (1600) despite dropping to a lower current
        # rating than P2 — position is by peak, not current.
        assert by_profile[1]["points"][-1] == {"position": 1, "peak_rating": 1600}
        assert by_profile[2]["points"][-1] == {"position": 2, "peak_rating": 1500}

    async def test_past_bucket_unchanged_by_later_peak(
        self, client: AsyncClient, session: AsyncSession
    ):
        # The append-only invariant: a later new high must not rewrite an
        # earlier bucket's peak.
        session.add(make_player(1))
        for match_id, day, rating in ((1, 1, 1500), (2, 3, 1800)):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 6, day, 12, 0, tzinfo=UTC),
                completed_at=datetime(2026, 6, day, 12, 30, tzinfo=UTC),
            )
            match.players.append(make_match_player(match_id, profile_id=1, new_rating=rating))
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1],
                leaderboard_id=3,
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        points = (await client.get("/v1/tournaments/cup/standings/history")).json()["players"][0][
            "points"
        ]
        peaks = [p["peak_rating"] for p in points if p["peak_rating"] is not None]
        # Append-only: the peak is never rewritten — monotonic non-decreasing,
        # the first real value is 1500 (not retroactively 1800), ending at 1800.
        assert peaks == sorted(peaks)
        assert peaks[0] == 1500
        assert peaks[-1] == 1800

    async def test_windowed_to_tournament_dates(self, client: AsyncClient, session: AsyncSession):
        session.add(make_player(1))
        for match_id, day, rating in (
            (1, 1, 2000),  # pre-window (start 06-05) — excluded
            (2, 6, 1500),  # in-window
            (3, 20, 3000),  # post-window (gf 06-10) — excluded
        ):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 6, day, 12, 0, tzinfo=UTC),
                completed_at=datetime(2026, 6, day, 12, 30, tzinfo=UTC),
            )
            match.players.append(make_match_player(match_id, profile_id=1, new_rating=rating))
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1],
                leaderboard_id=3,
                start_date=datetime(2026, 6, 5, tzinfo=UTC),
                grand_finals_date=datetime(2026, 6, 10, tzinfo=UTC),
            )
        )
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/standings/history")).json()
        points = body["players"][0]["points"]
        peaks = [p["peak_rating"] for p in points]
        # Only the 06-06 in-window match counts — the 06-01 pre-window (2000)
        # and 06-15 post-window (3000) matches are excluded: the final peak is
        # 1500, and no bucket ever shows 2000/3000.
        assert points[-1]["peak_rating"] == 1500
        assert all(p in (None, 1500) for p in peaks)
        # The window starts before the first in-window game, so the opening
        # bucket carries a null peak.
        assert peaks[0] is None

    async def test_team_combined_peak_series(self, client: AsyncClient, session: AsyncSession):
        # Team Alpha = P1+P2, Bravo = P3. Combined peak = sum of member peaks.
        for profile_id in (1, 2, 3):
            session.add(make_player(profile_id))
        for match_id, profile_id, rating in ((1, 1, 1500), (2, 2, 1600), (3, 3, 2000)):
            match = make_match(
                match_id,
                leaderboard_id=3,
                started_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                completed_at=datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(match_id, profile_id=profile_id, new_rating=rating)
            )
            session.add(match)
        tournament = make_tournament(
            "cup",
            profile_ids=[1, 2, 3],
            leaderboard_id=3,
            start_date=datetime(2026, 6, 1, tzinfo=UTC),
        )
        tournament.teams.append(make_team(tournament, "Alpha", profile_ids=[1, 2]))
        tournament.teams.append(make_team(tournament, "Bravo", profile_ids=[3]))
        session.add(tournament)
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/standings/history")).json()
        teams = {t["team_id"]: t for t in body["teams"]}
        alpha_id = tournament.teams[0].id
        bravo_id = tournament.teams[1].id
        # Final bucket: Alpha 1500+1600=3100 (pos1); Bravo 2000 (pos2).
        assert teams[alpha_id]["points"][-1] == {"position": 1, "combined_peak_elo": 3100}
        assert teams[bravo_id]["points"][-1] == {"position": 2, "combined_peak_elo": 2000}
        # Each team series carries its display strings (same shape as
        # StandingTeam), so the FE legend needs no /teams/standings join.
        assert (teams[alpha_id]["name"], teams[alpha_id]["initials"]) == ("Alpha", "ALPHA")
        assert (teams[bravo_id]["name"], teams[bravo_id]["initials"]) == ("Bravo", "BRAVO")

    async def test_emits_marker_at_intraday_shift(self, client: AsyncClient, session: AsyncSession):
        # A reorder mid-day produces a bucket stamped at the match-completion
        # time, not just at daily midnights (#226 per-shift granularity).
        session.add(make_player(1))
        session.add(make_player(2))
        for profile_id, hour, rating in ((1, 14, 1500), (2, 15, 1600)):
            match = make_match(
                profile_id,
                leaderboard_id=3,
                started_at=datetime(2026, 6, 1, hour, 0, tzinfo=UTC),
                completed_at=datetime(2026, 6, 1, hour, 0, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(profile_id, profile_id=profile_id, new_rating=rating)
            )
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1, 2],
                leaderboard_id=3,
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/standings/history")).json()
        # At least one intra-day marker (not a midnight) — the shift at 15:00.
        assert any("T00:00:00" not in bucket for bucket in body["buckets"])
        by_profile = {p["profile_id"]: p for p in body["players"]}
        # P2 (1600) overtakes P1 (1500) by the final bucket.
        assert by_profile[2]["points"][-1]["position"] == 1
        assert by_profile[1]["points"][-1]["position"] == 2

    async def test_ranks_by_all_time_peak_carried_in(
        self, client: AsyncClient, session: AsyncSession
    ):
        # A player carrying a higher lifetime peak (max_rating) they haven't
        # matched in-event still outranks one with a lower max_rating but a
        # higher in-event rating — by peak, matching the table (#226, the
        # kings-gauntlet KnOff case).
        for profile_id, max_rating, iw_rating in ((1, 1268, 1186), (2, 1208, 1190)):
            player = make_player(profile_id)
            player.ratings.append(
                make_player_rating(
                    profile_id, leaderboard_id=3, current_rating=iw_rating, max_rating=max_rating
                )
            )
            session.add(player)
            match = make_match(
                profile_id,
                leaderboard_id=3,
                started_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                completed_at=datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
            )
            match.players.append(
                make_match_player(profile_id, profile_id=profile_id, new_rating=iw_rating)
            )
            session.add(match)
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1, 2],
                leaderboard_id=3,
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        by_profile = {
            p["profile_id"]: p
            for p in (await client.get("/v1/tournaments/cup/standings/history")).json()["players"]
        }
        # By peak P1 (1268) > P2 (1208); by in-event rating P2 (1190) would have
        # outranked P1 (1186) — the disagreement this fixes.
        assert by_profile[1]["points"][-1] == {"position": 1, "peak_rating": 1268}
        assert by_profile[2]["points"][-1] == {"position": 2, "peak_rating": 1208}

    async def test_unrated_member_holds_tail_position(
        self, client: AsyncClient, session: AsyncSession
    ):
        # An unrated roster member (no rating on this leaderboard, e.g. jabo)
        # still appears with a position — at the name-sorted tail, null peak.
        rated = make_player(1)
        rated.ratings.append(
            make_player_rating(1, leaderboard_id=3, current_rating=2000, max_rating=2000)
        )
        session.add(rated)
        match = make_match(
            1,
            leaderboard_id=3,
            started_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
        )
        match.players.append(make_match_player(1, profile_id=1, new_rating=2000))
        session.add(match)
        # profile 2 is polled but has no PlayerRating row → unrated-but-polled:
        # its Player row passes the /standings visibility gate, yet it ranks at
        # the tail with a null peak. (A linked row with *no* Player row is the
        # distinct gated-out case — see test_entity_set_matches_standings_*.)
        session.add(make_player(2))
        session.add(
            make_tournament(
                "cup",
                profile_ids=[1, 2],
                leaderboard_id=3,
                start_date=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        by_profile = {
            p["profile_id"]: p
            for p in (await client.get("/v1/tournaments/cup/standings/history")).json()["players"]
        }
        assert by_profile[1]["points"][-1] == {"position": 1, "peak_rating": 2000}
        assert by_profile[2]["points"][-1] == {"position": 2, "peak_rating": None}

    async def test_entity_set_matches_standings_excludes_linked_unpolled(
        self, client: AsyncClient, session: AsyncSession
    ):
        # Regression for #232: history must chart exactly the entities the
        # standings table shows. A row linked to a profile_id whose Player
        # hasn't been polled yet is gated out of /standings; before the fix it
        # still surfaced in /standings/history as an unlabelled phantom the FE
        # couldn't join to a name/team. The three roster shapes below must
        # resolve to the same entity set on both surfaces.
        polled = make_player(1)  # linked + polled → in both
        polled.ratings.append(
            make_player_rating(1, leaderboard_id=3, current_rating=1500, max_rating=1500)
        )
        session.add(polled)
        match = make_match(
            1,
            leaderboard_id=3,
            started_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 6, 1, 12, 30, tzinfo=UTC),
        )
        match.players.append(make_match_player(1, profile_id=1, new_rating=1500))
        session.add(match)
        tournament = make_tournament(
            "cup", profile_ids=[1], leaderboard_id=3, start_date=datetime(2026, 6, 1, tzinfo=UTC)
        )
        # profile 2: linked but NOT yet polled (no Player row) → the phantom.
        tournament.tracked_players.append(TournamentPlayer(profile_id=2, name="p2"))
        # an unlinked placeholder (no profile_id) → first-class, must appear.
        tournament.tracked_players.append(TournamentPlayer(name="Newcomer"))
        session.add(tournament)
        await session.commit()

        standings = (await client.get("/v1/tournaments/cup/standings")).json()["items"]
        history = (await client.get("/v1/tournaments/cup/standings/history")).json()["players"]
        standings_ids = {row["tournament_player_id"] for row in standings}
        history_ids = {p["tournament_player_id"] for p in history}
        # The two surfaces agree on the entity set — no phantom in history.
        assert history_ids == standings_ids
        # The linked-but-unpolled profile 2 is in neither surface.
        assert 2 not in {row["profile_id"] for row in standings}
        assert 2 not in {p["profile_id"] for p in history}
        # The polled player and the unlinked placeholder are in both.
        polled_tp = next(r["tournament_player_id"] for r in standings if r["profile_id"] == 1)
        placeholder_tp = next(
            r["tournament_player_id"] for r in standings if r["name"] == "Newcomer"
        )
        assert {polled_tp, placeholder_tp} == history_ids
        # The series is self-describing even for an unlinked placeholder: it
        # carries its display name with no polled identity to join against.
        history_by_tp = {p["tournament_player_id"]: p for p in history}
        assert history_by_tp[placeholder_tp]["name"] == "Newcomer"

    async def test_empty_when_no_history(self, client: AsyncClient, session: AsyncSession):
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        body = (await client.get("/v1/tournaments/cup/standings/history")).json()
        assert body == {"last_polled_at": None, "buckets": [], "players": [], "teams": []}

    async def test_cache_control_header_unauthenticated(
        self, client: AsyncClient, session: AsyncSession
    ):
        session.add(make_tournament("cup"))
        await session.commit()
        response = await client.get("/v1/tournaments/cup/standings/history")
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

    async def test_surfaces_title_and_category(self, client: AsyncClient, session: AsyncSession):
        """stream_title + stream_category fold onto the row from the snapshot (#233)."""
        player = make_player(1, alias="p1")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        session.add(tournament)
        await session.flush()
        session.add(
            LiveStream(
                tournament_player_id=tournament.tracked_players[0].id,
                platform="twitch",
                title="ladder grind",
                category="Age of Empires II",
            )
        )
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["stream_live"] is True
        assert row["stream_title"] == "ladder grind"
        assert row["stream_category"] == "Age of Empires II"

    async def test_title_and_category_null_when_offline(
        self, client: AsyncClient, session: AsyncSession
    ):
        player = make_player(1, alias="p1")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        session.add(make_tournament("cup", profile_ids=[1], leaderboard_id=3))
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["stream_live"] is False
        assert row["stream_title"] is None
        assert row["stream_category"] is None

    async def test_twitch_metadata_wins_over_youtube(
        self, client: AsyncClient, session: AsyncSession
    ):
        """Live on both platforms → Twitch's title/category win (it has the category, #233)."""
        player = make_player(1, alias="p1")
        player.ratings.append(make_player_rating(1, leaderboard_id=3, current_rating=2000))
        session.add(player)
        tournament = make_tournament("cup", profile_ids=[1], leaderboard_id=3)
        session.add(tournament)
        await session.flush()
        row_id = tournament.tracked_players[0].id
        session.add(LiveStream(tournament_player_id=row_id, platform="youtube", title="yt mirror"))
        session.add(
            LiveStream(
                tournament_player_id=row_id,
                platform="twitch",
                title="twitch primary",
                category="Age of Empires II",
            )
        )
        await session.commit()

        row = (await client.get("/v1/tournaments/cup/standings")).json()["items"][0]
        assert row["stream_live"] is True
        assert row["stream_title"] == "twitch primary"
        assert row["stream_category"] == "Age of Empires II"


class TestCivilizationNamesCache:
    """``_civilization_names`` caches the static civ table in-process.

    The reference table is worker-written and changes only on a game patch, so
    the read endpoints share one cached id→name map instead of re-reading it per
    request (Sentry AOE2-LIVE-STANDINGS-API-1G).
    """

    async def test_caches_until_reset(self, session: AsyncSession):
        session.add(Civilization(civilization_id=7, name="Burgundians"))
        await session.commit()

        # First read populates the cache from the table.
        assert await _civilization_names(session) == {7: "Burgundians"}

        # A civ inserted after the cache is warm stays invisible within the TTL
        # — the table is not re-read on every call.
        session.add(Civilization(civilization_id=23, name="Japanese"))
        await session.commit()
        assert await _civilization_names(session) == {7: "Burgundians"}

        # Resetting (what the TTL lapse does in prod) re-reads the table.
        _reset_civilization_names_cache()
        assert await _civilization_names(session) == {7: "Burgundians", 23: "Japanese"}
