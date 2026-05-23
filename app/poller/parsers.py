"""Pure functions: upstream Relic JSON -> dicts ready for the upsert helpers.

Kept separate from the polling tasks so they can be unit-tested without
touching httpx or the database. Every parser returns lists of plain
``dict[str, Any]`` rows; the upsert helpers in ``app.poller.upserts`` take
those dicts directly.

Upstream reference: ``docs/data-sources.md``. The Relic ``/community/*``
surface returns rich JSON with snake_case keys; we normalize names and
types here (unix epoch -> UTC datetime, ``-1`` rank sentinels -> ``None``,
Steam ID extraction from ``name`` URLs, etc.).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models.match import MatchOutcome, MatchState

# Upstream uses -1 to mean "no rank on this leaderboard" (player never
# qualified). We normalize to None so the rank columns can carry a clean
# "absent" signal.
_UNRANKED_SENTINEL = -1

# `findAdvertisements` returns an integer `state` field. We've only
# directly observed `state=0` in the spike (all lobbies were staging);
# the higher values are inferred from upstream conventions. Revisit
# during the first tournament dress-rehearsal — see open questions in
# docs/data-sources.md.
_LIVE_STATE_STAGING = 0


def _from_unix(seconds: int | None) -> datetime | None:
    """Turn an upstream unix-epoch-seconds field into a UTC datetime, or None."""
    if not seconds:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def _none_if_unranked(value: int | None) -> int | None:
    """Upstream sentinel ``-1`` (unranked) becomes ``None``."""
    if value is None or value == _UNRANKED_SENTINEL:
        return None
    return value


def _extract_steam_id(name: str | None) -> str | None:
    """Parse the trailing Steam ID from ``name`` (e.g. ``/steam/7656...``)."""
    if not name:
        return None
    prefix = "/steam/"
    if name.startswith(prefix):
        return name[len(prefix) :] or None
    return None


def parse_player_stats(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a ``GetPersonalStat`` payload into (player_rows, rating_rows).

    Batched calls (multiple ``profile_ids``) return multiple ``statGroups``
    and a flat ``leaderboardStats`` list — rows are linked back to their
    owning profile via ``statgroup_id``. The 1v1 ladder uses solo
    statgroups (one member per group), which is the only shape v1 needs;
    team-statgroup behavior is a v1.x concern.
    """
    statgroup_to_profile: dict[int, int] = {}
    players: list[dict[str, Any]] = []
    for group in payload.get("statGroups", []):
        members = group.get("members") or []
        if not members:
            continue
        member = members[0]
        profile_id = member["profile_id"]
        statgroup_to_profile[group["id"]] = profile_id
        players.append(
            {
                "profile_id": profile_id,
                "alias": member.get("alias", ""),
                "country": member.get("country"),
                "steam_id": _extract_steam_id(member.get("name")),
                "level": member.get("level", 0),
                "xp": member.get("xp", 0),
                "region_id": member.get("leaderboardregion_id", 0),
                "clan_name": member.get("clanlist_name") or None,
            }
        )

    ratings: list[dict[str, Any]] = []
    for row in payload.get("leaderboardStats", []):
        profile_id = statgroup_to_profile.get(row.get("statgroup_id"))
        if profile_id is None:
            # leaderboardStats row points at an unknown statgroup — happens
            # if the payload is malformed; skip rather than crash the cycle.
            continue
        ratings.append(
            {
                "profile_id": profile_id,
                "leaderboard_id": row["leaderboard_id"],
                "current_rating": row.get("rating", 0),
                "max_rating": row.get("highestrating", 0),
                "wins": row.get("wins", 0),
                "losses": row.get("losses", 0),
                "streak": row.get("streak", 0),
                "drops": row.get("drops", 0),
                "rank": _none_if_unranked(row.get("rank")),
                "rank_total": _none_if_unranked(row.get("ranktotal")),
                "region_rank": _none_if_unranked(row.get("regionrank")),
                "region_rank_total": _none_if_unranked(row.get("regionranktotal")),
                "last_match_at": _from_unix(row.get("lastmatchdate")),
            }
        )

    return players, ratings


def parse_recent_matches(
    payload: dict[str, Any],
    leaderboard_for_matchtype: dict[int, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn a ``getRecentMatchHistory`` payload into (match_rows, match_player_rows).

    ``leaderboard_for_matchtype`` maps Relic's ``matchtype_id`` to the
    canonical ``leaderboard_id`` (e.g. 6 -> 3 for 1v1 RM Ranked). Sourced
    from the cached ``getAvailableLeaderboards`` snapshot; pass ``None``
    when the cache hasn't been loaded yet and we'll write ``None`` for the
    derived ``leaderboard_id`` column.
    """
    mapping = leaderboard_for_matchtype or {}

    matches: list[dict[str, Any]] = []
    players: list[dict[str, Any]] = []
    for raw in payload.get("matchHistoryStats", []):
        match_id = raw["id"]
        completed_at = _from_unix(raw.get("completiontime"))
        # `getRecentMatchHistory` is documented as showing only finished
        # matches; if an in-progress match ever surfaces here (open
        # question from the spike), `completed_at` would be None and we
        # mark it accordingly rather than guessing.
        state = MatchState.COMPLETED if completed_at is not None else MatchState.IN_PROGRESS
        matches.append(
            {
                "match_id": match_id,
                "map_name": raw.get("mapname", ""),
                "matchtype_id": raw.get("matchtype_id", 0),
                "leaderboard_id": mapping.get(raw.get("matchtype_id")),
                "started_at": _from_unix(raw.get("startgametime")),
                "completed_at": completed_at,
                "description": raw.get("description"),
                "state": state,
            }
        )

        # Two parallel per-player arrays — `matchhistoryreportresults` for
        # outcome/civ/team/xp, `matchhistorymember` for Elo deltas. Merge
        # them by profile_id so each MatchPlayer row carries the full set.
        members_by_profile: dict[int, dict[str, Any]] = {
            m["profile_id"]: m for m in raw.get("matchhistorymember", [])
        }
        for result in raw.get("matchhistoryreportresults", []):
            profile_id = result["profile_id"]
            member = members_by_profile.get(profile_id, {})
            players.append(
                {
                    "match_id": match_id,
                    "profile_id": profile_id,
                    "civilization_id": result.get("civilization_id", 0),
                    "team_id": result.get("teamid", 0),
                    "outcome": _parse_outcome(result.get("resulttype")),
                    "old_rating": member.get("oldrating"),
                    "new_rating": member.get("newrating"),
                    "xp_gained": result.get("xpgained", 0),
                }
            )

    return matches, players


def _parse_outcome(resulttype: int | None) -> MatchOutcome | None:
    """Map Relic's ``resulttype`` to our MatchOutcome enum.

    Relic uses ``1`` for win and ``0`` for loss on this surface; other
    values (e.g. drop, ongoing) round-trip as ``None`` so the row can be
    written without guessing.
    """
    if resulttype == 1:
        return MatchOutcome.WIN
    if resulttype == 0:
        return MatchOutcome.LOSS
    return None


def parse_live_advertisements(
    payload: dict[str, Any],
    tracked_profile_ids: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ``findAdvertisements`` into (match_rows, live_player_rows).

    Keeps only lobbies that include a tracked profile. ``match_rows`` are
    Match-shaped, suitable for ``upsert_match_from_live``. ``live_player_rows``
    link each *tracked* member to that match (``{match_id, profile_id}``) and
    feed ``replace_live_match_players`` — they back the ``in_match`` flag on
    the standings rows.

    No ``MatchPlayer`` rows are written from here: advertisement members are
    mid-game data, and final per-player values (Elo deltas, outcome) only
    land in ``getRecentMatchHistory`` once the match ends. The live-player
    rows deliberately carry no such data — they are a bare presence link.
    """
    matches: list[dict[str, Any]] = []
    live_players: list[dict[str, Any]] = []
    for ad in payload.get("advertisements", []):
        members = ad.get("matchmembers") or []
        tracked_members = [
            m["profile_id"] for m in members if m.get("profile_id") in tracked_profile_ids
        ]
        if not tracked_members:
            continue
        match_id = ad["match_id"]
        matches.append(
            {
                "match_id": match_id,
                "map_name": ad.get("mapname", ""),
                "matchtype_id": ad.get("matchtype_id", 0),
                # Live data doesn't carry the leaderboard mapping — the
                # recent-matches feed fills `leaderboard_id` once the
                # match completes.
                "leaderboard_id": None,
                # Lobby creation time as a placeholder; the real
                # `startgametime` lands when recent-matches sees the
                # completed match.
                "started_at": _from_unix(ad.get("creation_time")) or datetime.now(tz=UTC),
                "completed_at": None,
                "description": ad.get("description"),
                "state": (
                    MatchState.STAGING
                    if ad.get("state", _LIVE_STATE_STAGING) == _LIVE_STATE_STAGING
                    else MatchState.IN_PROGRESS
                ),
            }
        )
        live_players.extend(
            {"match_id": match_id, "profile_id": profile_id} for profile_id in tracked_members
        )
    return matches, live_players


def parse_available_leaderboards(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract leaderboard rows from ``getAvailableLeaderboards``.

    Returns ``{leaderboard_id, name, is_ranked, matchtypes}`` dicts suitable
    for ``upsert_leaderboard``. ``matchtypes`` carries the list of upstream
    matchtype IDs this leaderboard covers — the recent-matches poller reads
    it to rebuild its ``matchtype_id -> leaderboard_id`` map.
    """
    rows: list[dict[str, Any]] = []
    for lb in payload.get("leaderboards", []):
        # matchtypes[] entries are sometimes ints, sometimes dicts wrapping
        # an id — be defensive (mirrors matchtype_to_leaderboard_map).
        matchtypes = [
            mt["id"] if isinstance(mt, dict) else int(mt) for mt in lb.get("matchtypes", []) or []
        ]
        rows.append(
            {
                "leaderboard_id": lb["id"],
                "name": lb.get("name", ""),
                "is_ranked": bool(lb.get("isranked", 0)),
                "matchtypes": matchtypes,
            }
        )
    return rows


def matchtype_to_leaderboard_map(payload: dict[str, Any]) -> dict[int, int]:
    """Build a ``matchtype_id -> leaderboard_id`` lookup from getAvailableLeaderboards.

    Each leaderboard entry's ``matchtypes[]`` lists the matchtype IDs it
    covers; flattening gives the inverse map used by the recent-matches
    parser to derive ``Match.leaderboard_id``.
    """
    mapping: dict[int, int] = {}
    for lb in payload.get("leaderboards", []):
        leaderboard_id = lb["id"]
        for mt in lb.get("matchtypes", []) or []:
            # Each entry may be an int or a dict — be defensive.
            if isinstance(mt, dict):
                mapping[mt["id"]] = leaderboard_id
            else:
                mapping[int(mt)] = leaderboard_id
    return mapping
