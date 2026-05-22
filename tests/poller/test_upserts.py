"""Upsert helpers against the SQLite test DB.

These cover the *semantic* behavior of the three flavors of upsert —
full overwrite (recent), selective state update (live), and
insert-or-skip (match player) — including the critical guard that the
live poller can never roll back a completed match.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LiveMatchPlayer, Match, MatchPlayer, MatchState, Player, PlayerRating
from app.poller.upserts import (
    replace_live_match_players,
    upsert_match_from_live,
    upsert_match_from_recent,
    upsert_match_player,
    upsert_player,
    upsert_player_rating,
)


def _player_data(profile_id: int = 1, **overrides) -> dict:
    base = {
        "profile_id": profile_id,
        "alias": "Hera",
        "country": "ca",
        "steam_id": None,
        "level": 1,
        "xp": 0,
        "region_id": 0,
        "clan_name": None,
    }
    base.update(overrides)
    return base


def _rating_data(**overrides) -> dict:
    base = {
        "profile_id": 1,
        "leaderboard_id": 3,
        "current_rating": 1500,
        "max_rating": 1500,
        "wins": 0,
        "losses": 0,
        "streak": 0,
        "drops": 0,
        "rank": None,
        "rank_total": None,
        "region_rank": None,
        "region_rank_total": None,
        "last_match_at": None,
    }
    base.update(overrides)
    return base


def _match_data(match_id: int = 1, **overrides) -> dict:
    base = {
        "match_id": match_id,
        "map_name": "Arabia.rms",
        "matchtype_id": 6,
        "leaderboard_id": 3,
        "started_at": datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC),
        "completed_at": datetime(2026, 5, 18, 12, 30, 0, tzinfo=UTC),
        "description": None,
        "state": MatchState.COMPLETED,
    }
    base.update(overrides)
    return base


class TestUpsertPlayer:
    async def test_inserts_new(self, session: AsyncSession):
        await upsert_player(session, _player_data(profile_id=1, alias="first"))
        await session.commit()
        loaded = (await session.execute(select(Player))).scalar_one()
        assert loaded.alias == "first"

    async def test_overwrites_existing_on_conflict(self, session: AsyncSession):
        await upsert_player(session, _player_data(profile_id=1, alias="first"))
        await upsert_player(session, _player_data(profile_id=1, alias="second", level=99))
        await session.commit()
        loaded = (await session.execute(select(Player))).scalar_one()
        assert loaded.alias == "second"
        assert loaded.level == 99


class TestUpsertPlayerRating:
    async def test_composite_pk_upsert(self, session: AsyncSession):
        session.add(Player(**_player_data(profile_id=1)))
        await session.commit()

        await upsert_player_rating(session, _rating_data(current_rating=1500))
        await upsert_player_rating(session, _rating_data(current_rating=1800))
        await session.commit()

        loaded = (await session.execute(select(PlayerRating))).scalar_one()
        assert loaded.current_rating == 1800


class TestUpsertMatchFromRecent:
    async def test_inserts_new_match(self, session: AsyncSession):
        await upsert_match_from_recent(session, _match_data(match_id=1))
        await session.commit()
        loaded = (await session.execute(select(Match))).scalar_one()
        assert loaded.match_id == 1
        assert loaded.state == MatchState.COMPLETED

    async def test_overwrites_existing_match(self, session: AsyncSession):
        await upsert_match_from_recent(session, _match_data(match_id=1, map_name="Arabia.rms"))
        await upsert_match_from_recent(session, _match_data(match_id=1, map_name="Arena.rms"))
        await session.commit()
        loaded = (await session.execute(select(Match))).scalar_one()
        assert loaded.map_name == "Arena.rms"


class TestUpsertMatchFromLive:
    async def test_inserts_new_match_in_staging_state(self, session: AsyncSession):
        await upsert_match_from_live(
            session,
            _match_data(match_id=1, state=MatchState.STAGING, completed_at=None),
        )
        await session.commit()
        loaded = (await session.execute(select(Match))).scalar_one()
        assert loaded.state == MatchState.STAGING

    async def test_advances_state_on_conflict_when_not_completed(self, session: AsyncSession):
        await upsert_match_from_live(
            session,
            _match_data(match_id=1, state=MatchState.STAGING, completed_at=None),
        )
        await upsert_match_from_live(
            session,
            _match_data(match_id=1, state=MatchState.IN_PROGRESS, completed_at=None),
        )
        await session.commit()
        loaded = (await session.execute(select(Match))).scalar_one()
        assert loaded.state == MatchState.IN_PROGRESS

    async def test_does_not_overwrite_completed_match(self, session: AsyncSession):
        """Critical guard: a stale advertisement must not roll a completed match back."""
        await upsert_match_from_recent(
            session,
            _match_data(match_id=1, state=MatchState.COMPLETED),
        )
        await session.commit()

        await upsert_match_from_live(
            session,
            _match_data(match_id=1, state=MatchState.IN_PROGRESS, completed_at=None),
        )
        await session.commit()

        loaded = (await session.execute(select(Match))).scalar_one()
        assert loaded.state == MatchState.COMPLETED
        assert loaded.completed_at is not None

    async def test_live_upsert_does_not_clobber_non_state_columns(self, session: AsyncSession):
        """When updating an existing match, only `state` changes — `map_name` etc. stay."""
        await upsert_match_from_recent(
            session,
            _match_data(match_id=1, map_name="Arabia.rms", completed_at=None),
        )
        await session.commit()

        await upsert_match_from_live(
            session,
            _match_data(
                match_id=1,
                map_name="OverwriteMe.rms",
                state=MatchState.IN_PROGRESS,
                completed_at=None,
            ),
        )
        await session.commit()

        loaded = (await session.execute(select(Match))).scalar_one()
        assert loaded.map_name == "Arabia.rms"
        assert loaded.state == MatchState.IN_PROGRESS


class TestUpsertMatchPlayer:
    async def test_inserts_new_row(self, session: AsyncSession):
        session.add(Match(**_match_data(match_id=1)))
        await session.commit()

        await upsert_match_player(
            session,
            {
                "match_id": 1,
                "profile_id": 199325,
                "civilization_id": 16,
                "team_id": 1,
                "outcome": None,
                "old_rating": 1500,
                "new_rating": 1510,
                "xp_gained": 1,
            },
        )
        await session.commit()
        loaded = (await session.execute(select(MatchPlayer))).scalar_one()
        assert loaded.profile_id == 199325

    async def test_does_nothing_on_conflict(self, session: AsyncSession):
        """The first write wins — repeated polls of the same match are no-ops."""
        session.add(Match(**_match_data(match_id=1)))
        await session.commit()

        await upsert_match_player(
            session,
            {
                "match_id": 1,
                "profile_id": 1,
                "civilization_id": 16,
                "team_id": 0,
                "outcome": None,
                "old_rating": 1500,
                "new_rating": 1510,
                "xp_gained": 1,
            },
        )
        await upsert_match_player(
            session,
            {
                "match_id": 1,
                "profile_id": 1,
                "civilization_id": 99,
                "team_id": 99,
                "outcome": None,
                "old_rating": 9999,
                "new_rating": 9999,
                "xp_gained": 9999,
            },
        )
        await session.commit()

        loaded = (await session.execute(select(MatchPlayer))).scalar_one()
        # First write wins.
        assert loaded.civilization_id == 16
        assert loaded.new_rating == 1510


class TestReplaceLiveMatchPlayers:
    async def test_inserts_rows(self, session: AsyncSession):
        session.add(Match(**_match_data(match_id=1)))
        await session.commit()

        await replace_live_match_players(session, [{"match_id": 1, "profile_id": 7}])
        await session.commit()

        rows = (await session.execute(select(LiveMatchPlayer))).scalars().all()
        assert [(r.match_id, r.profile_id) for r in rows] == [(1, 7)]

    async def test_empty_rows_clears_table(self, session: AsyncSession):
        session.add(Match(**_match_data(match_id=1)))
        await session.commit()

        await replace_live_match_players(session, [{"match_id": 1, "profile_id": 7}])
        await session.commit()
        await replace_live_match_players(session, [])
        await session.commit()

        rows = (await session.execute(select(LiveMatchPlayer))).scalars().all()
        assert rows == []

    async def test_replaces_previous_snapshot(self, session: AsyncSession):
        session.add(Match(**_match_data(match_id=1)))
        session.add(Match(**_match_data(match_id=2)))
        await session.commit()

        await replace_live_match_players(session, [{"match_id": 1, "profile_id": 7}])
        await session.commit()
        await replace_live_match_players(session, [{"match_id": 2, "profile_id": 8}])
        await session.commit()

        rows = (await session.execute(select(LiveMatchPlayer))).scalars().all()
        assert [(r.match_id, r.profile_id) for r in rows] == [(2, 8)]

    async def test_duplicate_pairs_are_absorbed(self, session: AsyncSession):
        session.add(Match(**_match_data(match_id=1)))
        await session.commit()

        await replace_live_match_players(
            session,
            [{"match_id": 1, "profile_id": 7}, {"match_id": 1, "profile_id": 7}],
        )
        await session.commit()

        rows = (await session.execute(select(LiveMatchPlayer))).scalars().all()
        assert [(r.match_id, r.profile_id) for r in rows] == [(1, 7)]
