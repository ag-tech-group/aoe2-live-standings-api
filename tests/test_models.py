"""ORM smoke tests: create + read each model, verify relationships load."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Match,
    MatchOutcome,
    MatchPlayer,
    MatchState,
    Player,
    PlayerRating,
    Tournament,
    TournamentPlayer,
)


class TestPlayer:
    async def test_create_with_rating_and_load_via_relationship(self, session: AsyncSession):
        player = Player(
            profile_id=199325,
            alias="VIT | Hera",
            country="ca",
            steam_id="76561198449406083",
            level=4,
            xp=12964,
            region_id=3,
            clan_name="",
        )
        player.ratings.append(
            PlayerRating(
                leaderboard_id=3,
                current_rating=2788,
                max_rating=3045,
                wins=4962,
                losses=1701,
                streak=31,
                drops=55,
                rank=5,
                rank_total=47807,
                region_rank=1,
                region_rank_total=9639,
                last_match_at=datetime.fromtimestamp(1779084162, tz=UTC),
            )
        )
        session.add(player)
        await session.commit()

        stmt = (
            select(Player).where(Player.profile_id == 199325).options(selectinload(Player.ratings))
        )
        loaded = (await session.execute(stmt)).scalar_one()

        assert loaded.alias == "VIT | Hera"
        assert loaded.country == "ca"
        assert len(loaded.ratings) == 1
        rating = loaded.ratings[0]
        assert rating.current_rating == 2788
        assert rating.max_rating == 3045
        assert rating.streak == 31

    async def test_nullable_fields_accept_none(self, session: AsyncSession):
        player = Player(
            profile_id=1,
            alias="newbie",
            country=None,
            steam_id=None,
            level=1,
            xp=0,
            region_id=0,
            clan_name=None,
        )
        session.add(player)
        await session.commit()

        loaded = (await session.execute(select(Player).where(Player.profile_id == 1))).scalar_one()
        assert loaded.country is None
        assert loaded.steam_id is None
        assert loaded.clan_name is None


class TestMatch:
    async def test_create_with_players_roundtrip_outcome_enum(self, session: AsyncSession):
        match = Match(
            match_id=309483878,
            map_name="Kawasan.rms",
            matchtype_id=26,
            leaderboard_id=13,
            started_at=datetime.fromtimestamp(1714418066, tz=UTC),
            completed_at=datetime.fromtimestamp(1714418840, tz=UTC),
            state=MatchState.COMPLETED,
            players=[
                MatchPlayer(
                    profile_id=199325,
                    civilization_id=16,
                    team_id=1,
                    outcome=MatchOutcome.WIN,
                    old_rating=1969,
                    new_rating=1979,
                    xp_gained=1,
                ),
                MatchPlayer(
                    profile_id=409748,
                    civilization_id=19,
                    team_id=0,
                    outcome=MatchOutcome.LOSS,
                    old_rating=1824,
                    new_rating=1814,
                    xp_gained=1,
                ),
            ],
        )
        session.add(match)
        await session.commit()

        stmt = select(Match).where(Match.match_id == 309483878).options(selectinload(Match.players))
        loaded = (await session.execute(stmt)).scalar_one()

        assert loaded.state == MatchState.COMPLETED
        assert loaded.map_name == "Kawasan.rms"
        assert len(loaded.players) == 2
        winner = next(p for p in loaded.players if p.outcome == MatchOutcome.WIN)
        assert winner.profile_id == 199325
        assert winner.new_rating == 1979

    async def test_in_progress_match_has_nullable_completion_and_outcome(
        self, session: AsyncSession
    ):
        match = Match(
            match_id=1,
            map_name="Arabia.rms",
            matchtype_id=6,
            leaderboard_id=3,
            started_at=datetime.fromtimestamp(1779000000, tz=UTC),
            completed_at=None,
            state=MatchState.IN_PROGRESS,
            players=[
                MatchPlayer(
                    profile_id=199325,
                    civilization_id=16,
                    team_id=1,
                    outcome=None,
                    old_rating=None,
                    new_rating=None,
                    xp_gained=0,
                ),
            ],
        )
        session.add(match)
        await session.commit()

        loaded = (await session.execute(select(Match).where(Match.match_id == 1))).scalar_one()
        assert loaded.completed_at is None
        assert loaded.state == MatchState.IN_PROGRESS

    async def test_cascade_delete_match_removes_match_players(self, session: AsyncSession):
        match = Match(
            match_id=42,
            map_name="Arena.rms",
            matchtype_id=6,
            leaderboard_id=3,
            started_at=datetime.fromtimestamp(1779000000, tz=UTC),
            state=MatchState.COMPLETED,
            players=[
                MatchPlayer(
                    profile_id=1,
                    civilization_id=0,
                    team_id=0,
                    outcome=MatchOutcome.WIN,
                    old_rating=1000,
                    new_rating=1010,
                    xp_gained=1,
                ),
            ],
        )
        session.add(match)
        await session.commit()

        await session.delete(match)
        await session.commit()

        remaining = (
            (await session.execute(select(MatchPlayer).where(MatchPlayer.match_id == 42)))
            .scalars()
            .all()
        )
        assert remaining == []


class TestTournament:
    async def test_create_with_players_and_load_via_relationship(self, session: AsyncSession):
        tournament = Tournament(
            slug="hera-invitational-2026",
            name="Hera Streamer Invitational 2026",
            leaderboard_id=3,
            start_date=datetime(2026, 6, 1, tzinfo=UTC),
            grand_finals_date=datetime(2026, 6, 14, tzinfo=UTC),
        )
        tournament.tracked_players.append(TournamentPlayer(profile_id=199325, name="Hera"))
        tournament.tracked_players.append(TournamentPlayer(profile_id=347269, name="TaToH"))
        session.add(tournament)
        await session.commit()

        stmt = (
            select(Tournament)
            .where(Tournament.slug == "hera-invitational-2026")
            .options(selectinload(Tournament.tracked_players))
        )
        loaded = (await session.execute(stmt)).scalar_one()

        assert loaded.name == "Hera Streamer Invitational 2026"
        assert loaded.leaderboard_id == 3
        assert sorted(p.profile_id for p in loaded.tracked_players) == [199325, 347269]

    async def test_dates_are_optional(self, session: AsyncSession):
        session.add(Tournament(slug="undated", name="Undated", leaderboard_id=3))
        await session.commit()

        loaded = (
            await session.execute(select(Tournament).where(Tournament.slug == "undated"))
        ).scalar_one()
        assert loaded.start_date is None
        assert loaded.grand_finals_date is None

    async def test_cascade_delete_tournament_removes_players(self, session: AsyncSession):
        tournament = Tournament(slug="temp", name="Temp", leaderboard_id=3)
        tournament.tracked_players.append(TournamentPlayer(profile_id=1, name="p1"))
        session.add(tournament)
        await session.commit()

        await session.delete(tournament)
        await session.commit()

        remaining = (await session.execute(select(TournamentPlayer))).scalars().all()
        assert remaining == []
