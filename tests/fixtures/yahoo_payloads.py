"""Recorded-shape Yahoo Fantasy API payloads.

Yahoo's JSON is XML translated literally: numbered string keys standing in for
repeated elements, lists that mix dicts with scalars, and the same information
nested at different depths depending on the endpoint. Parsing it is the part of
the adapter most likely to break, and it is unreachable from the test suite —
the API needs OAuth, and a live call would make the tests depend on a network
and someone else's uptime.

So these are the shapes, reproduced faithfully enough to exercise the parsers:
the awkward nesting is the point, not an accident of how they were written.

They are *shape* fixtures, not a recording of one real league. If Yahoo changes
its response structure these keep passing while the real thing breaks — which
is why the adapter walks the payload defensively rather than indexing fixed
depths, and why the first live sync is worth eyeballing.
"""

from __future__ import annotations

STAT_CATEGORIES = {
    "fantasy_content": {
        "game": [
            {"game_key": "nfl", "name": "Football"},
            {"stat_categories": {"stats": [
                {"stat": {"stat_id": 4, "name": "Passing Yards",
                          "display_name": "Pass Yds",
                          "position_types": [{"position_type": "O"}]}},
                {"stat": {"stat_id": 5, "name": "Passing Touchdowns",
                          "position_types": [{"position_type": "O"}]}},
                {"stat": {"stat_id": 6, "name": "Interceptions",
                          "position_types": [{"position_type": "O"}]}},
                {"stat": {"stat_id": 9, "name": "Rushing Yards",
                          "position_types": [{"position_type": "O"}]}},
                {"stat": {"stat_id": 10, "name": "Rushing Touchdowns",
                          "position_types": [{"position_type": "O"}]}},
                {"stat": {"stat_id": 11, "name": "Receptions",
                          "position_types": [{"position_type": "O"}]}},
                {"stat": {"stat_id": 12, "name": "Reception Yards",
                          "position_types": [{"position_type": "O"}]}},
                {"stat": {"stat_id": 13, "name": "Reception Touchdowns",
                          "position_types": [{"position_type": "O"}]}},
                {"stat": {"stat_id": 16, "name": "2-Point Conversions",
                          "position_types": [{"position_type": "O"}]}},
                {"stat": {"stat_id": 18, "name": "Fumbles Lost",
                          "position_types": [{"position_type": "O"}]}},
                {"stat": {"stat_id": 19, "name": "Field Goals 0-19 Yards",
                          "position_types": [{"position_type": "K"}]}},
                {"stat": {"stat_id": 22, "name": "Field Goals 40-49 Yards",
                          "position_types": [{"position_type": "K"}]}},
                {"stat": {"stat_id": 23, "name": "Field Goals 50+ Yards",
                          "position_types": [{"position_type": "K"}]}},
                {"stat": {"stat_id": 26, "name": "Field Goals Missed 0-19 Yards",
                          "position_types": [{"position_type": "K"}]}},
                {"stat": {"stat_id": 29, "name": "Point After Attempt Made",
                          "position_types": [{"position_type": "K"}]}},
                {"stat": {"stat_id": 30, "name": "Point After Attempt Missed",
                          "position_types": [{"position_type": "K"}]}},
                {"stat": {"stat_id": 31, "name": "Sack",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 32, "name": "Interception",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 35, "name": "Touchdown",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 36, "name": "Safety",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 37, "name": "Block Kick",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 50, "name": "Points Allowed 0 points",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 51, "name": "Points Allowed 1-6 points",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 52, "name": "Points Allowed 7-13 points",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 53, "name": "Points Allowed 14-20 points",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 54, "name": "Points Allowed 21-27 points",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 55, "name": "Points Allowed 28-34 points",
                          "position_types": [{"position_type": "DT"}]}},
                {"stat": {"stat_id": 57, "name": "Points Allowed 35+ points",
                          "position_types": [{"position_type": "DT"}]}},
                # Something this app has no concept of, on purpose: it must be
                # reported as unmapped rather than guessed at or dropped.
                {"stat": {"stat_id": 78, "name": "Targets Inside The 10",
                          "position_types": [{"position_type": "O"}]}},
            ]}},
        ]
    }
}

#: A real league's settings: 6-point passing TDs, full PPR, yards-per-point
#: scoring, and banded points allowed. Deliberately the Keg South rules, so
#: the test can assert against a config that is known to be correct.
SETTINGS = {
    "fantasy_content": {
        "league": [
            {"league_key": "nfl.l.184206", "name": "Keg South Fantasy Fball League",
             "season": "2026", "num_teams": 10},
            {"settings": [{
                "draft_type": "live",
                "roster_positions": [
                    {"roster_position": {"position": "QB", "count": 1}},
                    {"roster_position": {"position": "RB", "count": 2}},
                    {"roster_position": {"position": "WR", "count": 2}},
                    {"roster_position": {"position": "TE", "count": 1}},
                    {"roster_position": {"position": "W/R/T", "count": 1}},
                    {"roster_position": {"position": "K", "count": 1}},
                    {"roster_position": {"position": "DEF", "count": 1}},
                    {"roster_position": {"position": "BN", "count": 6}},
                ],
                "stat_modifiers": {"stats": [
                    {"stat": {"stat_id": "4", "value": "0.02"}},
                    {"stat": {"stat_id": "5", "value": "6"}},
                    {"stat": {"stat_id": "6", "value": "-2"}},
                    {"stat": {"stat_id": "9", "value": "0.05"}},
                    {"stat": {"stat_id": "10", "value": "6"}},
                    {"stat": {"stat_id": "11", "value": "1"}},
                    {"stat": {"stat_id": "12", "value": "0.05"}},
                    {"stat": {"stat_id": "13", "value": "6"}},
                    {"stat": {"stat_id": "18", "value": "-2"}},
                    {"stat": {"stat_id": "19", "value": "3"}},
                    {"stat": {"stat_id": "22", "value": "4"}},
                    {"stat": {"stat_id": "23", "value": "5"}},
                    {"stat": {"stat_id": "26", "value": "-3"}},
                    {"stat": {"stat_id": "29", "value": "1"}},
                    {"stat": {"stat_id": "30", "value": "-3"}},
                    {"stat": {"stat_id": "31", "value": "1"}},
                    {"stat": {"stat_id": "32", "value": "2"}},
                    {"stat": {"stat_id": "35", "value": "6"}},
                    {"stat": {"stat_id": "36", "value": "2"}},
                    {"stat": {"stat_id": "37", "value": "2"}},
                    {"stat": {"stat_id": "50", "value": "10"}},
                    {"stat": {"stat_id": "51", "value": "7"}},
                    {"stat": {"stat_id": "52", "value": "4"}},
                    {"stat": {"stat_id": "53", "value": "2"}},
                    {"stat": {"stat_id": "54", "value": "0"}},
                    {"stat": {"stat_id": "55", "value": "-1"}},
                    {"stat": {"stat_id": "57", "value": "-4"}},
                    {"stat": {"stat_id": "78", "value": "0.5"}},
                ]},
                "num_playoff_teams": "6",
                "playoff_start_week": "15",
                "end_week": "17",
                "waiver_type": "FR",
                "waiver_time": "1",
                "uses_faab": "0",
            }]},
        ]
    }
}

TEAMS = {
    "fantasy_content": {
        "league": [
            {"league_key": "nfl.l.184206", "name": "Keg South Fantasy Fball League",
             "season": "2026"},
            {"teams": {
                "0": {"team": [[
                    {"team_key": "nfl.l.184206.t.1"},
                    {"name": "☠DIESEL☠"},
                    {"managers": [{"manager": {"nickname": "Phil"}}]},
                ]]},
                "1": {"team": [[
                    {"team_key": "nfl.l.184206.t.7"},
                    {"name": "The Killer Ps"},
                    {"managers": [{"manager": {"nickname": "Pat"}}]},
                ]]},
                "count": 2,
            }},
        ]
    }
}

SCOREBOARD = {
    "fantasy_content": {
        "league": [
            {"league_key": "nfl.l.184206"},
            {"scoreboard": {
                "0": {"matchups": {
                    "0": {"matchup": {
                        "week": "3", "status": "postevent", "is_tied": 0,
                        "teams": {
                            "0": {"team": [
                                [{"team_key": "nfl.l.184206.t.1"}, {"name": "DIESEL"}],
                                {"team_points": {"coverage_type": "week", "week": "3",
                                                 "total": "118.42"}},
                            ]},
                            "1": {"team": [
                                [{"team_key": "nfl.l.184206.t.7"}, {"name": "The Killer Ps"}],
                                {"team_points": {"coverage_type": "week", "week": "3",
                                                 "total": "102.80"}},
                            ]},
                            "count": 2,
                        },
                    }},
                    "1": {"matchup": {
                        "week": "3", "status": "postevent", "is_tied": 0,
                        "teams": {
                            "0": {"team": [
                                [{"team_key": "nfl.l.184206.t.2"}, {"name": "Ruly's Big Tymers"}],
                                {"team_points": {"total": "96.10"}},
                            ]},
                            "1": {"team": [
                                [{"team_key": "nfl.l.184206.t.5"}, {"name": "Code Brown"}],
                                {"team_points": {"total": "99.30"}},
                            ]},
                            "count": 2,
                        },
                    }},
                    "count": 2,
                }},
                "week": "3",
            }},
        ]
    }
}

#: A week still being played: no totals yet, status is preevent.
SCOREBOARD_PENDING = {
    "fantasy_content": {
        "league": [
            {"league_key": "nfl.l.184206"},
            {"scoreboard": {
                "0": {"matchups": {
                    "0": {"matchup": {
                        "week": "4", "status": "preevent",
                        "teams": {
                            "0": {"team": [
                                [{"team_key": "nfl.l.184206.t.1"}, {"name": "DIESEL"}],
                                {"team_points": {"total": ""}},
                            ]},
                            "1": {"team": [
                                [{"team_key": "nfl.l.184206.t.2"}, {"name": "Ruly's Big Tymers"}],
                                {"team_points": {"total": ""}},
                            ]},
                            "count": 2,
                        },
                    }},
                    "count": 1,
                }},
                "week": "4",
            }},
        ]
    }
}

TRANSACTIONS = {
    "fantasy_content": {
        "league": [
            {"league_key": "nfl.l.184206"},
            {"transactions": {
                "0": {"transaction": [
                    {"transaction_key": "nfl.l.184206.tr.11", "type": "add/drop",
                     "status": "successful", "timestamp": "1757000000",
                     "faab_bid": "14"},
                    {"players": {
                        "0": {"player": [
                            [{"player_key": "nfl.p.100"},
                             {"name": {"full": "C.J. Stroud"}},
                             {"editorial_team_abbr": "HOU"},
                             {"display_position": "QB"}],
                            {"transaction_data": [{
                                "type": "add", "source_type": "freeagents",
                                "destination_type": "team",
                                "destination_team_key": "nfl.l.184206.t.1"}]},
                        ]},
                        # Yahoo sometimes gives transaction_data as a bare dict
                        # and sometimes as a single-element list. Both here.
                        "1": {"player": [
                            [{"player_key": "nfl.p.101"},
                             {"name": {"full": "Harrison Butker"}},
                             {"editorial_team_abbr": "KC"},
                             {"display_position": "K"}],
                            {"transaction_data": {
                                "type": "drop", "source_type": "team",
                                "source_team_key": "nfl.l.184206.t.1",
                                "destination_type": "waivers"}},
                        ]},
                        "count": 2,
                    }},
                ]},
                "1": {"transaction": [
                    {"transaction_key": "nfl.l.184206.tr.12", "type": "trade",
                     "status": "successful", "timestamp": "1757600000"},
                    {"players": {
                        "0": {"player": [
                            [{"player_key": "nfl.p.102"},
                             {"name": {"full": "Puka Nacua"}},
                             {"editorial_team_abbr": "LAR"},
                             {"display_position": "WR"}],
                            {"transaction_data": [{
                                "type": "add",
                                "source_team_key": "nfl.l.184206.t.4",
                                "destination_team_key": "nfl.l.184206.t.1"}]},
                        ]},
                        "1": {"player": [
                            [{"player_key": "nfl.p.103"},
                             {"name": {"full": "Bijan Robinson"}},
                             {"editorial_team_abbr": "ATL"},
                             {"display_position": "RB"}],
                            {"transaction_data": [{
                                "type": "drop",
                                "source_team_key": "nfl.l.184206.t.1",
                                "destination_team_key": "nfl.l.184206.t.4"}]},
                        ]},
                        "count": 2,
                    }},
                ]},
                # A vetoed move: present in the feed, but it never happened.
                "2": {"transaction": [
                    {"transaction_key": "nfl.l.184206.tr.13", "type": "add",
                     "status": "vetoed", "timestamp": "1757700000"},
                    {"players": {
                        "0": {"player": [
                            [{"player_key": "nfl.p.104"}, {"name": {"full": "Ghost Player"}}],
                            {"transaction_data": [{
                                "type": "add",
                                "destination_team_key": "nfl.l.184206.t.9"}]},
                        ]},
                        "count": 1,
                    }},
                ]},
                "count": 3,
            }},
        ]
    }
}

DRAFT_RESULTS = {
    "fantasy_content": {
        "league": [
            {"league_key": "nfl.l.184206"},
            {"draft_results": {
                "0": {"draft_result": {"pick": 1, "round": 1,
                                       "team_key": "nfl.l.184206.t.3",
                                       "player_key": "nfl.p.200"}},
                "1": {"draft_result": {"pick": 2, "round": 1,
                                       "team_key": "nfl.l.184206.t.1",
                                       "player_key": "nfl.p.201"}},
                "2": {"draft_result": {"pick": 3, "round": 1,
                                       "team_key": "nfl.l.184206.t.5",
                                       "player_key": "nfl.p.202"}},
                "count": 3,
            }},
        ]
    }
}

PLAYERS_BY_KEY = {
    "fantasy_content": {
        "league": [
            {"league_key": "nfl.l.184206"},
            {"players": {
                "0": {"player": [[
                    {"player_key": "nfl.p.200"},
                    {"name": {"full": "Ja'Marr Chase", "first": "Ja'Marr", "last": "Chase"}},
                    {"editorial_team_abbr": "CIN"},
                    {"display_position": "WR"},
                    {"bye_weeks": {"week": "10"}},
                ]]},
                "1": {"player": [[
                    {"player_key": "nfl.p.201"},
                    {"name": {"full": "Bijan Robinson"}},
                    {"editorial_team_abbr": "ATL"},
                    {"display_position": "RB"},
                    {"bye_weeks": {"week": "5"}},
                ]]},
                "2": {"player": [[
                    {"player_key": "nfl.p.202"},
                    {"name": {"full": "Baltimore"}},
                    {"editorial_team_abbr": "BAL"},
                    {"display_position": "DEF"},
                    {"bye_weeks": {"week": "7"}},
                ]]},
                "count": 3,
            }},
        ]
    }
}

ROSTERS = {
    "fantasy_content": {
        "league": [
            {"league_key": "nfl.l.184206"},
            {"teams": {
                "0": {"team": [
                    [{"team_key": "nfl.l.184206.t.1"}, {"name": "DIESEL"}],
                    {"roster": {"0": {"players": {
                        "0": {"player": [[
                            {"player_key": "nfl.p.201"},
                            {"name": {"full": "Bijan Robinson"}},
                            {"editorial_team_abbr": "ATL"},
                            {"display_position": "RB"},
                        ]]},
                        "count": 1,
                    }}}},
                ]},
                "count": 1,
            }},
        ]
    }
}
