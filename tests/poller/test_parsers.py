"""Unit tests for the upstream-JSON parsers."""

from datetime import UTC, datetime

from app.models.match import UNKNOWN_CIVILIZATION_ID, MatchOutcome, MatchState
from app.poller.parsers import (
    matchtype_to_leaderboard_map,
    parse_available_leaderboards,
    parse_live_advertisements,
    parse_player_stats,
    parse_races,
    parse_recent_matches,
)


class TestParsePlayerStats:
    def test_single_profile_extracts_player_and_ratings(self):
        payload = {
            "statGroups": [
                {
                    "id": 100,
                    "members": [
                        {
                            "profile_id": 199325,
                            "alias": "VIT | Hera",
                            "name": "/steam/76561198449406083",
                            "country": "ca",
                            "level": 4,
                            "xp": 12964,
                            "leaderboardregion_id": 3,
                            "clanlist_name": "",
                        }
                    ],
                }
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
                    "ranktotal": 47807,
                    "regionrank": 1,
                    "regionranktotal": 9639,
                    "lastmatchdate": 1779084162,
                }
            ],
        }

        players, ratings = parse_player_stats(payload)

        assert len(players) == 1
        player = players[0]
        assert player["profile_id"] == 199325
        assert player["alias"] == "VIT | Hera"
        assert player["steam_id"] == "76561198449406083"
        assert player["country"] == "ca"

        assert len(ratings) == 1
        rating = ratings[0]
        assert rating["profile_id"] == 199325
        assert rating["leaderboard_id"] == 3
        assert rating["current_rating"] == 2788
        assert rating["max_rating"] == 3045
        assert rating["last_match_at"] == datetime.fromtimestamp(1779084162, tz=UTC)

    def test_batched_call_links_ratings_to_profiles_via_statgroup(self):
        payload = {
            "statGroups": [
                {"id": 100, "members": [{"profile_id": 1, "alias": "a"}]},
                {"id": 200, "members": [{"profile_id": 2, "alias": "b"}]},
            ],
            "leaderboardStats": [
                {"statgroup_id": 100, "leaderboard_id": 3, "rating": 2000},
                {"statgroup_id": 200, "leaderboard_id": 3, "rating": 1500},
                {"statgroup_id": 200, "leaderboard_id": 4, "rating": 1800},
            ],
        }

        players, ratings = parse_player_stats(payload)

        assert {p["profile_id"] for p in players} == {1, 2}
        by_profile = {(r["profile_id"], r["leaderboard_id"]): r for r in ratings}
        assert by_profile[(1, 3)]["current_rating"] == 2000
        assert by_profile[(2, 3)]["current_rating"] == 1500
        assert by_profile[(2, 4)]["current_rating"] == 1800

    def test_unranked_sentinels_become_none(self):
        payload = {
            "statGroups": [{"id": 1, "members": [{"profile_id": 1, "alias": "x"}]}],
            "leaderboardStats": [
                {
                    "statgroup_id": 1,
                    "leaderboard_id": 3,
                    "rank": -1,
                    "ranktotal": -1,
                    "regionrank": -1,
                    "regionranktotal": -1,
                }
            ],
        }

        _, ratings = parse_player_stats(payload)
        rating = ratings[0]
        assert rating["rank"] is None
        assert rating["rank_total"] is None
        assert rating["region_rank"] is None
        assert rating["region_rank_total"] is None

    def test_max_rating_floors_at_current_when_highestrating_missing(self):
        # Relic omits ``highestrating`` for accounts with no wins on a
        # ladder. Without the floor, ``max_rating`` lands at 0 and team
        # peak aggregates (see #158) tank for any team containing such
        # a player.
        payload = {
            "statGroups": [{"id": 1, "members": [{"profile_id": 1, "alias": "x"}]}],
            "leaderboardStats": [
                {"statgroup_id": 1, "leaderboard_id": 3, "rating": 819},
            ],
        }
        _, ratings = parse_player_stats(payload)
        assert ratings[0]["max_rating"] == 819

    def test_max_rating_floors_at_current_when_highestrating_is_zero(self):
        # Same shape as the previous test but with the field present
        # and explicitly 0 — also seen in the wild for fresh accounts.
        payload = {
            "statGroups": [{"id": 1, "members": [{"profile_id": 1, "alias": "x"}]}],
            "leaderboardStats": [
                {
                    "statgroup_id": 1,
                    "leaderboard_id": 3,
                    "rating": 819,
                    "highestrating": 0,
                },
            ],
        }
        _, ratings = parse_player_stats(payload)
        assert ratings[0]["max_rating"] == 819

    def test_no_steam_prefix_yields_none_steam_id(self):
        payload = {
            "statGroups": [
                {"id": 1, "members": [{"profile_id": 1, "alias": "x", "name": "weird"}]}
            ],
            "leaderboardStats": [],
        }
        players, _ = parse_player_stats(payload)
        assert players[0]["steam_id"] is None

    def test_clan_name_empty_string_becomes_none(self):
        payload = {
            "statGroups": [
                {"id": 1, "members": [{"profile_id": 1, "alias": "x", "clanlist_name": ""}]}
            ],
            "leaderboardStats": [],
        }
        players, _ = parse_player_stats(payload)
        assert players[0]["clan_name"] is None

    def test_orphaned_leaderboard_row_is_skipped(self):
        payload = {
            "statGroups": [{"id": 100, "members": [{"profile_id": 1, "alias": "a"}]}],
            "leaderboardStats": [
                {"statgroup_id": 100, "leaderboard_id": 3, "rating": 2000},
                {"statgroup_id": 999, "leaderboard_id": 3, "rating": 9999},
            ],
        }
        _, ratings = parse_player_stats(payload)
        assert len(ratings) == 1


class TestParseRecentMatches:
    def test_extracts_match_and_merges_per_player_arrays(self):
        payload = {
            "matchHistoryStats": [
                {
                    "id": 309483878,
                    "mapname": "Kawasan.rms",
                    "matchtype_id": 26,
                    "startgametime": 1714418066,
                    "completiontime": 1714418840,
                    "description": None,
                    "matchhistoryreportresults": [
                        {
                            "profile_id": 199325,
                            "civilization_id": 16,
                            "teamid": 1,
                            "resulttype": 1,
                            "xpgained": 1,
                        },
                        {
                            "profile_id": 409748,
                            "civilization_id": 19,
                            "teamid": 0,
                            "resulttype": 0,
                            "xpgained": 1,
                        },
                    ],
                    "matchhistorymember": [
                        {"profile_id": 199325, "oldrating": 1969, "newrating": 1979},
                        {"profile_id": 409748, "oldrating": 1824, "newrating": 1814},
                    ],
                }
            ]
        }

        matches, players, _ = parse_recent_matches(payload, leaderboard_for_matchtype={26: 13})

        assert len(matches) == 1
        match = matches[0]
        assert match["match_id"] == 309483878
        assert match["map_name"] == "Kawasan.rms"
        assert match["leaderboard_id"] == 13
        assert match["state"] == MatchState.COMPLETED
        assert match["completed_at"] == datetime.fromtimestamp(1714418840, tz=UTC)

        assert len(players) == 2
        winner = next(p for p in players if p["profile_id"] == 199325)
        assert winner["outcome"] == MatchOutcome.WIN
        assert winner["old_rating"] == 1969
        assert winner["new_rating"] == 1979
        assert winner["civilization_id"] == 16

    def test_map_name_resolved_from_options_blob(self):
        # ``mapname`` lies for ranked automatch (#265) — when the options
        # blob carries a verified map locstring id, it wins over mapname.
        from tests.poller.test_map_names import encode_options

        payload = {
            "matchHistoryStats": [
                {
                    "id": 484298540,
                    "mapname": "Marketplace.rms",
                    "matchtype_id": 6,
                    "options": encode_options({"10": "10878"}),
                    "startgametime": 1781089031,
                    "completiontime": 1781090921,
                    "matchhistoryreportresults": [],
                    "matchhistorymember": [],
                }
            ]
        }

        matches, _, _ = parse_recent_matches(payload)

        assert matches[0]["map_name"] == "Black Forest"

    def test_missing_completiontime_marks_in_progress(self):
        payload = {
            "matchHistoryStats": [
                {
                    "id": 1,
                    "mapname": "Arabia.rms",
                    "matchtype_id": 6,
                    "startgametime": 1779000000,
                    "completiontime": 0,
                    "matchhistoryreportresults": [],
                    "matchhistorymember": [],
                }
            ]
        }
        matches, _, _ = parse_recent_matches(payload)
        assert matches[0]["state"] == MatchState.IN_PROGRESS
        assert matches[0]["completed_at"] is None

    def test_unknown_matchtype_leaves_leaderboard_id_null(self):
        payload = {
            "matchHistoryStats": [
                {
                    "id": 1,
                    "mapname": "x.rms",
                    "matchtype_id": 999,
                    "startgametime": 1,
                    "completiontime": 2,
                    "matchhistoryreportresults": [],
                    "matchhistorymember": [],
                }
            ]
        }
        matches, _, _ = parse_recent_matches(payload, leaderboard_for_matchtype={6: 3})
        assert matches[0]["leaderboard_id"] is None

    def test_resulttype_outside_known_values_is_none(self):
        payload = {
            "matchHistoryStats": [
                {
                    "id": 1,
                    "mapname": "x.rms",
                    "matchtype_id": 6,
                    "startgametime": 1,
                    "completiontime": 2,
                    "matchhistoryreportresults": [
                        {"profile_id": 1, "resulttype": 99, "xpgained": 0}
                    ],
                    "matchhistorymember": [],
                }
            ]
        }
        _, players, _ = parse_recent_matches(payload)
        assert players[0]["outcome"] is None

    def test_extracts_aliases_from_profiles_array(self):
        # The top-level `profiles` array names every participant — including
        # untracked opponents — and a blank alias is dropped so it can't
        # overwrite a real name on a later poll (#349).
        payload = {
            "matchHistoryStats": [],
            "profiles": [
                {"profile_id": 199325, "alias": "TheViper"},
                {"profile_id": 409748, "alias": "Hera"},
                {"profile_id": 5, "alias": ""},
                {"profile_id": None, "alias": "ghost"},
                {"alias": "no-id"},
            ],
        }
        _, _, aliases = parse_recent_matches(payload)
        assert aliases == [
            {"profile_id": 199325, "alias": "TheViper"},
            {"profile_id": 409748, "alias": "Hera"},
        ]

    def test_missing_profiles_array_yields_no_aliases(self):
        _, _, aliases = parse_recent_matches({"matchHistoryStats": []})
        assert aliases == []


class TestParseLiveAdvertisements:
    def test_filters_to_tracked_profile_lobbies(self):
        payload = {
            "matches": [
                {
                    "id": 1,
                    "mapname": "Arabia.rms",
                    "matchtype_id": 0,
                    "state": 0,
                    "matchmembers": [{"profile_id": 199325}, {"profile_id": 409748}],
                },
                {
                    "id": 2,
                    "mapname": "Arena.rms",
                    "matchtype_id": 0,
                    "state": 0,
                    "matchmembers": [{"profile_id": 99}, {"profile_id": 100}],
                },
            ]
        }
        matches, live_players = parse_live_advertisements(payload, tracked_profile_ids={199325})
        assert [m["match_id"] for m in matches] == [1]
        # Only the tracked member of the kept lobby is linked.
        assert live_players == [{"match_id": 1, "profile_id": 199325}]

    def test_live_player_row_per_tracked_member(self):
        payload = {
            "matches": [
                {
                    "id": 1,
                    "mapname": "x.rms",
                    "matchtype_id": 0,
                    "state": 0,
                    "matchmembers": [
                        {"profile_id": 1},
                        {"profile_id": 2},
                        {"profile_id": 99},
                    ],
                }
            ]
        }
        _, live_players = parse_live_advertisements(payload, tracked_profile_ids={1, 2})
        assert sorted(p["profile_id"] for p in live_players) == [1, 2]
        assert all(p["match_id"] == 1 for p in live_players)

    def test_state_zero_maps_to_staging(self):
        payload = {
            "matches": [
                {
                    "id": 1,
                    "mapname": "x.rms",
                    "matchtype_id": 0,
                    "state": 0,
                    "matchmembers": [{"profile_id": 1}],
                }
            ]
        }
        matches, _ = parse_live_advertisements(payload, tracked_profile_ids={1})
        assert matches[0]["state"] == MatchState.STAGING

    def test_state_nonzero_maps_to_in_progress(self):
        payload = {
            "matches": [
                {
                    "id": 1,
                    "mapname": "x.rms",
                    "matchtype_id": 0,
                    "state": 2,
                    "matchmembers": [{"profile_id": 1}],
                }
            ]
        }
        matches, _ = parse_live_advertisements(payload, tracked_profile_ids={1})
        assert matches[0]["state"] == MatchState.IN_PROGRESS

    def test_empty_tracked_set_returns_empty(self):
        payload = {
            "matches": [
                {
                    "id": 1,
                    "mapname": "x.rms",
                    "matchtype_id": 0,
                    "state": 0,
                    "matchmembers": [{"profile_id": 1}],
                }
            ]
        }
        assert parse_live_advertisements(payload, tracked_profile_ids=set()) == ([], [])

    def test_legacy_advertisements_shape_still_parses(self):
        # The pre-rename shape (#267): list under `advertisements`, lobby id
        # as `match_id`, with a `creation_time`. Kept as a fallback.
        payload = {
            "advertisements": [
                {
                    "match_id": 7,
                    "mapname": "Arabia.rms",
                    "matchtype_id": 0,
                    "creation_time": 1779000000,
                    "state": 0,
                    "matchmembers": [{"profile_id": 1}],
                }
            ]
        }
        matches, live_players = parse_live_advertisements(payload, tracked_profile_ids={1})
        assert matches[0]["match_id"] == 7
        assert matches[0]["started_at"] == datetime.fromtimestamp(1779000000, tz=UTC)
        assert live_players == [{"match_id": 7, "profile_id": 1}]

    def test_map_name_resolved_from_options_blob(self):
        # Lobby `mapname` is usually the "my map" placeholder even when a
        # standard map is hosted; the options blob carries the real one
        # (#267, same mechanism as #265).
        from tests.poller.test_map_names import encode_options

        payload = {
            "matches": [
                {
                    "id": 1,
                    "mapname": "my map",
                    "matchtype_id": 0,
                    "state": 0,
                    "options": encode_options({"10": "10878"}),
                    "matchmembers": [{"profile_id": 1}],
                }
            ]
        }
        matches, _ = parse_live_advertisements(payload, tracked_profile_ids={1})
        assert matches[0]["map_name"] == "Black Forest"

    def test_lobby_without_id_is_skipped(self):
        payload = {
            "matches": [
                {
                    "mapname": "x.rms",
                    "matchtype_id": 0,
                    "state": 0,
                    "matchmembers": [{"profile_id": 1}],
                },
                {
                    "id": 2,
                    "mapname": "y.rms",
                    "matchtype_id": 0,
                    "state": 0,
                    "matchmembers": [{"profile_id": 1}],
                },
            ]
        }
        matches, live_players = parse_live_advertisements(payload, tracked_profile_ids={1})
        assert [m["match_id"] for m in matches] == [2]
        assert live_players == [{"match_id": 2, "profile_id": 1}]


class TestParseAvailableLeaderboards:
    def test_basic_extraction(self):
        payload = {
            "leaderboards": [
                {"id": 3, "name": "1v1 RM Ranked", "isranked": 1, "matchtypes": [6]},
                {"id": 99, "name": "Custom POM", "isranked": 0},
            ]
        }
        items = parse_available_leaderboards(payload)
        assert [i["leaderboard_id"] for i in items] == [3, 99]
        assert items[0]["is_ranked"] is True
        assert items[1]["is_ranked"] is False
        assert items[0]["matchtypes"] == [6]
        # Missing matchtypes in the upstream payload defaults to [].
        assert items[1]["matchtypes"] == []


class TestParseRaces:
    def test_maps_id_and_name(self):
        payload = {
            "races": [
                {"id": 7, "name": "Burgundians", "faction_id": 0, "locstringid": -1},
                {"id": 0, "name": "Armenians"},
            ]
        }
        assert parse_races(payload) == [
            {"civilization_id": 7, "name": "Burgundians"},
            {"civilization_id": 0, "name": "Armenians"},
        ]

    def test_empty_when_races_absent(self):
        assert parse_races({}) == []


class TestParseMatchPlayerCiv:
    def test_missing_civilization_id_uses_sentinel(self):
        # Relic civ ids start at 0 (Armenians), so a missing civ can't default
        # to 0 — it would masquerade as Armenians. It falls back to the
        # non-civ sentinel instead (#227).
        payload = {
            "matchHistoryStats": [
                {
                    "id": 1,
                    "mapname": "Arabia.rms",
                    "matchtype_id": 6,
                    "startgametime": 100,
                    "completiontime": 200,
                    "matchhistoryreportresults": [
                        {"profile_id": 1, "resulttype": 1, "teamid": 0, "xpgained": 5},
                    ],
                    "matchhistorymember": [],
                }
            ]
        }
        _, players, _ = parse_recent_matches(payload)
        assert players[0]["civilization_id"] == UNKNOWN_CIVILIZATION_ID

    def test_civ_prefers_member_over_report_zero_default(self):
        # `matchhistoryreportresults.civilization_id` silently falls back to 0
        # (Armenians) for some random-civ games that never resolved upstream;
        # `matchhistorymember.civilization_id` carries the real assignment.
        # Real case: match 485296798 — PiG ("pigrandom") played Bohemians (4)
        # but the report array said 0, so we rendered Armenians. Prefer the
        # member value. Ground truth verified against aoe2insights.
        payload = {
            "matchHistoryStats": [
                {
                    "id": 485296798,
                    "mapname": "Marketplace.rms",
                    "matchtype_id": 6,
                    "startgametime": 100,
                    "completiontime": 200,
                    "matchhistoryreportresults": [
                        {
                            "profile_id": 5735770,
                            "civilization_id": 0,
                            "teamid": 1,
                            "resulttype": 1,
                            "xpgained": 1,
                        },
                        {
                            "profile_id": 1434678,
                            "civilization_id": 34,
                            "teamid": 0,
                            "resulttype": 0,
                            "xpgained": 1,
                        },
                    ],
                    "matchhistorymember": [
                        {
                            "profile_id": 5735770,
                            "civilization_id": 4,
                            "oldrating": 1671,
                            "newrating": 1690,
                        },
                        {
                            "profile_id": 1434678,
                            "civilization_id": 34,
                            "oldrating": 1671,
                            "newrating": 1652,
                        },
                    ],
                }
            ]
        }
        _, players, _ = parse_recent_matches(payload)
        pig = next(p for p in players if p["profile_id"] == 5735770)
        opponent = next(p for p in players if p["profile_id"] == 1434678)
        assert pig["civilization_id"] == 4  # Bohemians, not 0/Armenians
        assert opponent["civilization_id"] == 34  # Portuguese (arrays agree)

    def test_civ_falls_back_to_report_when_member_lacks_civ(self):
        # A member row without a `civilization_id` (older payloads) must not
        # blank the civ — keep the report array's value rather than dropping
        # to the sentinel.
        payload = {
            "matchHistoryStats": [
                {
                    "id": 2,
                    "mapname": "Arabia.rms",
                    "matchtype_id": 6,
                    "startgametime": 100,
                    "completiontime": 200,
                    "matchhistoryreportresults": [
                        {
                            "profile_id": 1,
                            "civilization_id": 16,
                            "teamid": 0,
                            "resulttype": 1,
                            "xpgained": 5,
                        },
                    ],
                    "matchhistorymember": [
                        {"profile_id": 1, "oldrating": 1000, "newrating": 1010},
                    ],
                }
            ]
        }
        _, players, _ = parse_recent_matches(payload)
        assert players[0]["civilization_id"] == 16


class TestMatchtypeToLeaderboardMap:
    def test_flattens_int_matchtype_lists(self):
        payload = {
            "leaderboards": [
                {"id": 3, "matchtypes": [6, 7]},
                {"id": 4, "matchtypes": [8]},
            ]
        }
        assert matchtype_to_leaderboard_map(payload) == {6: 3, 7: 3, 8: 4}

    def test_flattens_dict_matchtype_lists(self):
        payload = {"leaderboards": [{"id": 3, "matchtypes": [{"id": 6}, {"id": 7}]}]}
        assert matchtype_to_leaderboard_map(payload) == {6: 3, 7: 3}
