"""End-to-end: respx-mocked upstream + real Postgres + real routers.

Drives one ``tick_*`` of each poller against the testcontainer Postgres,
then makes HTTP requests against the same DB and asserts the responses.
The lifespan-driven ``while True`` loops are skipped on purpose — the
unit tests already cover the loop scaffolding, and we want this suite
to stay deterministic.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.events import EventType, emit_nudge, hub, poll_for_nudges
from app.poller.leaderboards import load_leaderboards
from app.poller.live_matches import tick_live_matches
from app.poller.player_stats import tick_player_stats
from app.poller.recent_matches import tick_recent_matches
from tests.conftest import make_tournament

# We point the poller's httpx client at an obvious dummy URL so respx
# routes are unambiguous. The poller code uses whatever base_url the
# client was built with — settings.upstream_base_url is irrelevant when
# the test constructs the client directly.
_TEST_UPSTREAM = "https://upstream.test"

_HERA = 199325
_ACCM = 347269
_TRACKED_PROFILES = [_HERA, _ACCM]


def _leaderboards_payload() -> dict[str, Any]:
    return {
        "leaderboards": [
            {"id": 1, "name": "SOLO_DM_RANKED", "isranked": 1, "matchtypes": [4]},
            {"id": 3, "name": "SOLO_RM_RANKED", "isranked": 1, "matchtypes": [6]},
            {"id": 4, "name": "TEAM_RM_RANKED", "isranked": 1, "matchtypes": [7]},
        ]
    }


def _player_stats_payload() -> dict[str, Any]:
    return {
        "statGroups": [
            {
                "id": 100,
                "members": [
                    {
                        "profile_id": _HERA,
                        "alias": "VIT | Hera",
                        "name": "/steam/76561198449406083",
                        "country": "ca",
                        "level": 4,
                        "xp": 12964,
                        "leaderboardregion_id": 3,
                        "clanlist_name": "",
                    }
                ],
            },
            {
                "id": 200,
                "members": [
                    {
                        "profile_id": _ACCM,
                        "alias": "wR.ACCM",
                        "name": "/steam/76561198000000001",
                        "country": "au",
                        "level": 4,
                        "xp": 9100,
                        "leaderboardregion_id": 3,
                        "clanlist_name": "wR",
                    }
                ],
            },
        ],
        "leaderboardStats": [
            {
                "statgroup_id": 100,
                "leaderboard_id": 3,
                "rating": 2788,
                "highestrating": 3045,
                "wins": 4962,
                "losses": 1701,
                "streak": 31,
                "drops": 55,
                "rank": 5,
                "ranktotal": 47830,
                "regionrank": 1,
                "regionranktotal": 9639,
                "lastmatchdate": 1779084162,
            },
            {
                "statgroup_id": 100,
                "leaderboard_id": 4,
                "rating": 2000,
                "highestrating": 2100,
                "wins": 100,
                "losses": 50,
                "streak": 2,
                "drops": 1,
                "rank": 250,
                "ranktotal": 30000,
                "regionrank": 50,
                "regionranktotal": 5000,
                "lastmatchdate": 1779000000,
            },
            {
                "statgroup_id": 200,
                "leaderboard_id": 3,
                "rating": 2718,
                "highestrating": 2892,
                "wins": 3001,
                "losses": 1315,
                "streak": 7,
                "drops": 30,
                "rank": 14,
                "ranktotal": 47830,
                "regionrank": 2,
                "regionranktotal": 9639,
                "lastmatchdate": 1779000000,
            },
        ],
    }


def _recent_matches_payload() -> dict[str, Any]:
    """Two finished matches; match 1001 is Hera vs ACCM (shared between both profile queries)."""
    return {
        "matchHistoryStats": [
            {
                "id": 1001,
                "mapname": "Arabia.rms",
                "matchtype_id": 6,
                "startgametime": 1779000000,
                "completiontime": 1779001000,
                "description": None,
                "matchhistoryreportresults": [
                    {
                        "profile_id": _HERA,
                        "civilization_id": 16,
                        "teamid": 1,
                        "resulttype": 1,
                        "xpgained": 1,
                    },
                    {
                        "profile_id": _ACCM,
                        "civilization_id": 19,
                        "teamid": 0,
                        "resulttype": 0,
                        "xpgained": 1,
                    },
                ],
                "matchhistorymember": [
                    {"profile_id": _HERA, "oldrating": 2780, "newrating": 2788},
                    {"profile_id": _ACCM, "oldrating": 2726, "newrating": 2718},
                ],
            },
            {
                "id": 1002,
                "mapname": "Arena.rms",
                "matchtype_id": 6,
                "startgametime": 1778900000,
                "completiontime": 1778901000,
                "description": None,
                "matchhistoryreportresults": [
                    {
                        "profile_id": _HERA,
                        "civilization_id": 8,
                        "teamid": 1,
                        "resulttype": 1,
                        "xpgained": 1,
                    },
                    {
                        "profile_id": 999999,
                        "civilization_id": 12,
                        "teamid": 0,
                        "resulttype": 0,
                        "xpgained": 1,
                    },
                ],
                "matchhistorymember": [
                    {"profile_id": _HERA, "oldrating": 2770, "newrating": 2780},
                    {"profile_id": 999999, "oldrating": 1800, "newrating": 1790},
                ],
            },
        ]
    }


def _live_payload() -> dict[str, Any]:
    """Two lobbies; only the first involves a tracked player (Hera)."""
    return {
        "matches": [
            {
                "id": 9001,
                "mapname": "Arena.rms",
                "matchtype_id": 0,
                "state": 0,
                "matchmembers": [{"profile_id": _HERA}, {"profile_id": 888888}],
                "description": "Tournament practice",
            },
            {
                "id": 9002,
                "mapname": "Migration.rms",
                "matchtype_id": 0,
                "state": 0,
                "matchmembers": [{"profile_id": 555555}, {"profile_id": 666666}],
            },
        ]
    }


async def _drive_one_full_polling_cycle(
    session_maker: async_sessionmaker,
    *,
    leaderboards: dict[str, Any] | None = None,
    player_stats: dict[str, Any] | None = None,
    recent_matches: dict[str, Any] | None = None,
    live_advertisements: dict[str, Any] | None = None,
) -> None:
    """Run load_leaderboards + one tick of each of the three pollers.

    Caller passes JSON payloads (or ``None`` to skip mocking a route — handy
    for the failure-path test where a 500 is registered instead).
    """
    with respx.mock(base_url=_TEST_UPSTREAM) as mock:
        if leaderboards is not None:
            mock.get("/community/leaderboard/getAvailableLeaderboards").respond(json=leaderboards)
        if player_stats is not None:
            mock.get("/community/leaderboard/GetPersonalStat").respond(json=player_stats)
        if recent_matches is not None:
            mock.get("/community/leaderboard/getRecentMatchHistory").respond(json=recent_matches)
        if live_advertisements is not None:
            mock.get("/community/advertisement/findAdvertisements").respond(
                json=live_advertisements
            )

        async with httpx.AsyncClient(base_url=_TEST_UPSTREAM) as client:
            matchtype_map = await load_leaderboards(client, session_maker) if leaderboards else {}
            if player_stats:
                await tick_player_stats(client, _TRACKED_PROFILES, session_maker)
            if recent_matches:
                await tick_recent_matches(client, _TRACKED_PROFILES, session_maker, matchtype_map)
            if live_advertisements:
                await tick_live_matches(client, _TRACKED_PROFILES, session_maker)


@pytest.mark.asyncio
async def test_golden_path(pg_client: AsyncClient, patched_session_maker: async_sessionmaker):
    """One tick each → every /v1 endpoint serves the right shape and content."""
    await _drive_one_full_polling_cycle(
        patched_session_maker,
        leaderboards=_leaderboards_payload(),
        player_stats=_player_stats_payload(),
        recent_matches=_recent_matches_payload(),
        live_advertisements=_live_payload(),
    )

    async with patched_session_maker() as seed:
        seed.add(make_tournament("hera-cup", profile_ids=_TRACKED_PROFILES, leaderboard_id=3))
        await seed.commit()

    # 1. Leaderboards — cache loaded from the upstream snapshot.
    r = await pg_client.get("/v1/leaderboards")
    assert r.status_code == 200
    payload = r.json()
    assert {lb["leaderboard_id"] for lb in payload["items"]} == {1, 3, 4}
    assert payload["last_polled_at"] is not None

    # 2. Players — the tournament roster with embedded ratings.
    r = await pg_client.get("/v1/tournaments/hera-cup/players")
    assert r.status_code == 200
    items = r.json()["items"]
    # ASCII sort: "VIT | Hera" (V=86) < "wR.ACCM" (w=119) — Postgres default collation.
    assert [p["profile_id"] for p in items] == [_HERA, _ACCM]
    hera = next(p for p in items if p["profile_id"] == _HERA)
    assert hera["alias"] == "VIT | Hera"
    assert hera["steam_id"] == "76561198449406083"
    assert {r["leaderboard_id"] for r in hera["ratings"]} == {3, 4}

    # 3. Player detail — ratings + recent matches, addressed by surrogate id.
    r = await pg_client.get(f"/v1/tournaments/hera-cup/players/{hera['tournament_player_id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["alias"] == "VIT | Hera"
    assert len(detail["ratings"]) == 2
    # Hera appears in both completed matches (1001 and 1002).
    assert {m["match_id"] for m in detail["recent_matches"]} == {1001, 1002}

    # 4. Standings — the tournament's roster, sorted by current rating desc.
    r = await pg_client.get("/v1/tournaments/hera-cup/standings")
    assert r.status_code == 200
    rows = r.json()["items"]
    assert [row["profile_id"] for row in rows] == [_HERA, _ACCM]
    assert rows[0]["current_rating"] == 2788
    assert rows[1]["current_rating"] == 2718
    # Hera is in live lobby 9001 (staging) → in_match flips on the row;
    # ACCM is in no live lobby.
    assert (rows[0]["in_match"], rows[0]["live_match_id"]) == (True, 9001)
    assert (rows[1]["in_match"], rows[1]["live_match_id"]) == (False, None)
    # Hera won both completed matches inside the (open) tournament window —
    # counts/streak, peak (best of 2788 / 2780), and recent_matchups all reflect
    # in-window play; last_match_at is the newer of the two starts.
    record = rows[0]["tournament_record"]
    assert record["games_played"] == 2
    assert record["wins"] == 2
    assert record["losses"] == 0
    assert record["streak"] == 2
    assert record["longest_win_streak"] == 2
    assert record["peak_rating"] == 2788
    assert [m["outcome"] for m in record["recent_matchups"]] == ["win", "win"]
    assert record["win_pct"] == 100.0
    assert record["last_match_at"] is not None

    # 5. Matches — completed list. Match 1001 only exists once even though both
    # profile queries returned it — confirms ON CONFLICT dedupe.
    r = await pg_client.get("/v1/tournaments/hera-cup/matches")
    assert r.status_code == 200
    matches = r.json()["items"]
    assert {m["match_id"] for m in matches} >= {1001, 1002}
    # Match 1001 has exactly 2 MatchPlayer rows (Hera + ACCM).
    m1001 = next(m for m in matches if m["match_id"] == 1001)
    assert {p["profile_id"] for p in m1001["players"]} == {_HERA, _ACCM}

    # 6. Match detail.
    r = await pg_client.get("/v1/tournaments/hera-cup/matches/1001")
    assert r.status_code == 200
    detail = r.json()
    assert detail["map_name"] == "Arabia.rms"
    assert detail["state"] == "completed"
    winner = next(p for p in detail["players"] if p["outcome"] == "win")
    assert winner["profile_id"] == _HERA
    assert winner["new_rating"] == 2788

    # 7. Live — only the lobby with Hera, not the untracked one (9002).
    r = await pg_client.get("/v1/tournaments/hera-cup/live")
    assert r.status_code == 200
    live = r.json()["items"]
    assert [m["match_id"] for m in live] == [9001]
    assert live[0]["state"] == "staging"


@pytest.mark.asyncio
async def test_upstream_failure_isolated_to_one_poller(
    pg_client: AsyncClient, patched_session_maker: async_sessionmaker
):
    """When one upstream endpoint 500s, the other pollers + endpoints still work.

    In production the ``run_*`` wrapper catches and logs; calling ``tick_*``
    directly here surfaces the exception so we can verify both the
    propagation contract (``raise_for_status`` fires on 5xx) and that the
    rest of the system stays consistent — leaderboards still load, matches
    still flow, players stay empty.
    """
    with respx.mock(base_url=_TEST_UPSTREAM) as mock:
        mock.get("/community/leaderboard/getAvailableLeaderboards").respond(
            json=_leaderboards_payload()
        )
        mock.get("/community/leaderboard/GetPersonalStat").respond(500)
        mock.get("/community/leaderboard/getRecentMatchHistory").respond(
            json=_recent_matches_payload()
        )
        mock.get("/community/advertisement/findAdvertisements").respond(json=_live_payload())

        async with httpx.AsyncClient(base_url=_TEST_UPSTREAM) as client:
            matchtype_map = await load_leaderboards(client, patched_session_maker)

            with pytest.raises(httpx.HTTPStatusError):
                await tick_player_stats(client, _TRACKED_PROFILES, patched_session_maker)

            # Other pollers proceed independently — they share the same
            # client + container but have no data dependency on player_stats.
            await tick_recent_matches(
                client, _TRACKED_PROFILES, patched_session_maker, matchtype_map
            )
            await tick_live_matches(client, _TRACKED_PROFILES, patched_session_maker)

    async with patched_session_maker() as seed:
        seed.add(make_tournament("hera-cup", profile_ids=_TRACKED_PROFILES, leaderboard_id=3))
        await seed.commit()

    # Roster players have no rows yet because that poller failed.
    r = await pg_client.get("/v1/tournaments/hera-cup/players")
    assert r.status_code == 200
    assert r.json()["items"] == []

    # Matches landed despite the player_stats failure.
    r = await pg_client.get("/v1/tournaments/hera-cup/matches")
    assert r.status_code == 200
    assert len(r.json()["items"]) > 0

    # Live lobby still picked up.
    r = await pg_client.get("/v1/tournaments/hera-cup/live")
    assert r.status_code == 200
    assert [m["match_id"] for m in r.json()["items"]] == [9001]

    # Leaderboard cache populated.
    r = await pg_client.get("/v1/leaderboards")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 3

    # Standings — the roster has no ratings yet, so an empty envelope.
    r = await pg_client.get("/v1/tournaments/hera-cup/standings")
    assert r.status_code == 200
    assert r.json() == {"last_polled_at": None, "items": []}


@pytest.mark.asyncio
async def test_emit_nudge_drives_local_hub_via_poller(
    patched_session_maker: async_sessionmaker, monkeypatch
):
    """emit_nudge -> nudge_versions bump -> poll_for_nudges -> hub.publish.

    End-to-end check of the polling nudge spine (#196 Option B): a transaction's
    ``emit_nudge`` advances the event's ``polled_at`` on commit, and the
    per-instance poll loop sees the change through its pooled session and
    republishes to the local ``EventHub`` for SSE fan-out.
    """
    # Tighten the poll interval so the test doesn't wait the 2s prod cadence.
    monkeypatch.setattr("app.events._POLL_INTERVAL_SECONDS", 0.05)

    nudges = hub.subscribe()
    poller_task = asyncio.create_task(poll_for_nudges(patched_session_maker))
    try:
        # Let the first poll establish the baseline (startup never publishes).
        await asyncio.sleep(0.2)

        async with patched_session_maker() as session:
            await emit_nudge(session, EventType.STANDINGS)
            await session.commit()

        nudge = await asyncio.wait_for(nudges.get(), timeout=2.0)
        assert nudge.event == EventType.STANDINGS
    finally:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass
        hub.unsubscribe(nudges)
