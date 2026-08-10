"""The in-season loop: weekly numbers, the wire, results, and the log.

The draft is one night. Everything here runs for four months afterwards, and
the failures are quieter — a bye week counted as a normal week, a waiver add
ranked on points it would never score from the bench, a standings table that
disagrees with the site. Each of those is silent, so each gets a test.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ff import config as C
from ff import matchups, storage, transactions as T, waivers
from ff.players import Player, make_player_id, normalize_status, status_multiplier
from ff.pool import invalidate
from ff.storage.files import FileBackend
from ff.weekly import (
    availability,
    per_game,
    regular_season_weeks,
    season_weeks,
    week_pool,
)


def league(teams: int = 10, **overrides):
    cfg = C.new_config("Season Test", "manual", teams, 1.0)
    cfg["teams"] = [C.new_team(f"Team {i}") for i in range(1, teams + 1)]
    cfg["my_team_id"] = cfg["teams"][0]["id"]
    cfg.update(overrides)
    return cfg


def player(name, pos, points, season=None, **kw):
    """A player already through valuation, as the engines receive them."""
    p = Player(player_id=make_player_id(name, pos, ""), name=name, pos=pos, **kw)
    p.points = points
    p.season_points = points * 17 if season is None else season
    p.vor = points
    return p


# --------------------------------------------------------------------------
# The weekly pool
# --------------------------------------------------------------------------

HEADER = "Player,Team,Pos,Bye,Rush Yds,Rush TD,Rec,Rec Yds,Week"


def csv_rows(rows: list[tuple]) -> str:
    return "\n".join([HEADER] + [",".join(str(c) for c in row) for row in rows])


class TestWeekPool(unittest.TestCase):
    """Season projections in, one specific Sunday out."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.backend = FileBackend(
            leagues_dir=root / "leagues",
            projections_dir=root / "proj",
            db_path=root / "ff.db",
            secrets_dir=root / "secrets",
        )
        storage.set_backend(self.backend)
        invalidate()
        self.addCleanup(invalidate)
        self.addCleanup(storage.reset)

        self.cfg = league()
        self.backend.save_projection_set("season.csv", csv_rows([
            # name, team, pos, bye, rush yd, rush td, rec, rec yd, week
            ("Bye Back",   "ATL", "RB", 7, 1700, 17, 0,  0,   ""),
            ("Steady Back", "DAL", "RB", 9, 1360, 17, 0,  0,   ""),
            ("Third Back", "CHI", "RB", 5, 850,  8,  0,  0,   ""),
            ("Big Wideout", "CIN", "WR", 10, 0,  0,  100, 1300, ""),
            ("Slot Guy",   "LAR", "WR", 6,  0,   0,  85,  900,  ""),
        ]))

    def pool(self, week, statuses=None):
        return {p.player_id: p for p in week_pool(self.cfg, week, statuses)}

    def test_season_total_is_spread_across_the_games(self):
        """With no weekly file, a week is the season divided by the schedule."""
        by_id = self.pool(3)
        back = by_id["steady-back-RB"]
        self.assertAlmostEqual(back.points, per_game(back.season_points, self.cfg), places=6)
        self.assertGreater(back.season_points, back.points)

    def test_a_bye_week_scores_nothing(self):
        self.assertEqual(self.pool(7)["bye-back-RB"].points, 0.0)
        self.assertEqual(self.pool(7)["bye-back-RB"].status, "BYE")
        # ...and the same player is fine the week before.
        self.assertGreater(self.pool(6)["bye-back-RB"].points, 0.0)

    def test_a_bye_does_not_touch_the_season_projection(self):
        """Holding a player through his bye is normal; he is not worthless."""
        self.assertGreater(self.pool(7)["bye-back-RB"].season_points, 0.0)

    def test_status_override_zeroes_an_out_player(self):
        healthy = self.pool(3)["steady-back-RB"].points
        out = self.pool(3, {"steady-back-RB": "Out"})["steady-back-RB"]
        self.assertGreater(healthy, 0)
        self.assertEqual(out.points, 0.0)
        self.assertEqual(out.status, "O")
        self.assertFalse(out.playable)

    def test_questionable_is_discounted_not_removed(self):
        healthy = self.pool(3)["steady-back-RB"].points
        doubtful = self.pool(3, {"steady-back-RB": "Q"})["steady-back-RB"]
        self.assertLess(doubtful.points, healthy)
        self.assertGreater(doubtful.points, 0)
        self.assertTrue(doubtful.playable)

    def test_a_week_specific_projection_beats_the_average(self):
        self.backend.save_projection_set("week3.csv", csv_rows([
            ("Steady Back", "DAL", "RB", 9, 200, 3, 0, 0, 3),
        ]))
        invalidate()
        week3 = self.pool(3)["steady-back-RB"]
        week4 = self.pool(4)["steady-back-RB"]
        self.assertGreater(week3.points, week4.points)
        # The season number is untouched by a single week's file.
        self.assertAlmostEqual(week3.season_points, week4.season_points, places=6)

    def test_weekly_rows_do_not_leak_into_the_season_pool(self):
        """A week 3 row must not be mistaken for a season projection."""
        self.backend.save_projection_set("week3.csv", csv_rows([
            ("Steady Back", "DAL", "RB", 9, 200, 3, 0, 0, 3),
        ]))
        invalidate()
        from ff.pool import build_pool

        season = {p.player_id: p for p in build_pool(self.cfg)}
        self.assertGreater(season["steady-back-RB"].points, 100)

    def test_the_cached_season_pool_is_not_mutated(self):
        from ff.pool import build_pool

        before = {p.player_id: p.points for p in build_pool(self.cfg)}
        self.pool(7, {"steady-back-RB": "Out"})
        after = {p.player_id: p.points for p in build_pool(self.cfg)}
        self.assertEqual(before, after)


class TestAvailability(unittest.TestCase):
    def test_status_spellings_are_normalised(self):
        for raw, expected in [
            ("Out", "O"), ("OUT", "O"), ("questionable", "Q"), ("GTD", "Q"),
            ("Injured Reserve", "IR"), ("PUP", "IR"), ("", ""), ("Active", ""),
            ("nonsense", ""),
        ]:
            self.assertEqual(normalize_status(raw), expected, raw)

    def test_a_bye_outranks_a_healthy_status(self):
        p = player("Somebody", "RB", 10, bye=7)
        self.assertEqual(availability(p, 7), "BYE")
        self.assertEqual(availability(p, 8), "")

    def test_multipliers_are_configurable(self):
        cfg = {"valuation": {"status_multipliers": {"Q": 1.0}}}
        self.assertEqual(status_multiplier("Q", cfg), 1.0)
        self.assertLess(status_multiplier("Q", {}), 1.0)

    def test_season_weeks_follow_the_playoff_settings(self):
        cfg = league(playoffs={"teams": 6, "weeks": [15, 16, 17]})
        self.assertEqual(regular_season_weeks(cfg)[-1], 14)
        self.assertEqual(season_weeks(cfg)[-1], 17)
        cfg = league(playoffs={"teams": 4, "weeks": [16, 17]})
        self.assertEqual(regular_season_weeks(cfg)[-1], 15)


# --------------------------------------------------------------------------
# Waivers
# --------------------------------------------------------------------------

class TestWaivers(unittest.TestCase):
    """The wire is ranked on what it changes, not on who is best."""

    def setUp(self):
        self.cfg = league()
        # A full, ordinary roster: starters plus a deep WR room.
        self.roster = [
            player("QB One", "QB", 20),
            player("RB One", "RB", 18), player("RB Two", "RB", 15),
            player("WR One", "WR", 17), player("WR Two", "WR", 16),
            player("WR Three", "WR", 14), player("WR Four", "WR", 13),
            player("TE One", "TE", 11),
            player("K One", "K", 8),
            player("DST One", "DST", 7),
        ]

    def test_a_good_player_who_would_never_start_gains_nothing(self):
        """The whole point: a WR5 behind four better WRs is worth zero to you."""
        buried = player("WR Five", "WR", 12)
        result = waivers.recommend(self.cfg, self.roster, [buried], week=3)
        row = result["recommendations"][0]
        self.assertEqual(row["gain_week"], 0.0)
        self.assertIn("bench depth", row["reason"])

    def test_an_upgrade_over_a_starter_is_measured(self):
        upgrade = player("WR Star", "WR", 22)
        result = waivers.recommend(self.cfg, self.roster, [upgrade], week=3)
        row = result["recommendations"][0]
        # He does not simply replace the worst starting WR: he pushes everyone
        # down a slot, and WR Three (14) falls out of the FLEX. The lineup goes
        # 17+16+14 -> 22+17+16, so the gain is 8, not the 6-point gap to WR Two.
        # Measuring the lineup rather than the player is what catches this.
        self.assertAlmostEqual(row["gain_week"], 8.0, places=2)

    def test_a_full_roster_pairs_the_add_with_a_drop(self):
        self.cfg["roster"]["bench"] = 0            # 9 starters, no bench
        crowded = self.roster[:9]
        result = waivers.recommend(self.cfg, crowded, [player("WR Star", "WR", 22)])
        row = result["recommendations"][0]
        self.assertTrue(result["roster_full"])
        self.assertIsNotNone(row["drop"])

    def test_the_drop_is_the_least_valuable_of_the_free_ones(self):
        """Several drops always tie at 'costs nothing today'. The tie has to
        break on the player's own value or the tool suggests cutting a star."""
        self.cfg["roster"]["bench"] = 0
        crowded = self.roster[:8] + [player("Stashed Stud", "RB", 2, season=280)]
        result = waivers.recommend(self.cfg, crowded, [player("K Two", "K", 9)])
        dropped = result["recommendations"][0]["drop"]
        self.assertIsNotNone(dropped)
        self.assertNotEqual(dropped["name"], "Stashed Stud")

    def test_filling_an_empty_slot_beats_a_marginal_upgrade(self):
        no_kicker = [p for p in self.roster if p.pos != "K"]
        result = waivers.recommend(
            self.cfg, no_kicker,
            [player("K Free", "K", 8), player("WR Slight", "WR", 16.5)],
        )
        self.assertEqual(result["recommendations"][0]["player"]["pos"], "K")
        self.assertTrue(result["recommendations"][0]["fills_gap"])
        self.assertEqual(result["unfilled_slots"], {"K": 1})

    def test_rest_of_season_and_this_week_can_disagree(self):
        """A handcuff whose starter just went down: nothing today, plenty later."""
        stash = player("Backup Back", "RB", 0.0, season=500)   # bye or inactive
        result = waivers.recommend(self.cfg, self.roster, [stash], week=3)
        row = result["recommendations"][0]
        self.assertEqual(row["gain_week"], 0.0)
        self.assertGreater(row["gain_rest_of_season"], 0.0)

    def test_faab_is_none_when_the_league_does_not_use_it(self):
        result = waivers.recommend(self.cfg, self.roster, [player("WR X", "WR", 22)])
        self.assertIsNone(result["faab"])
        self.assertIsNone(result["recommendations"][0]["bid"])

    def test_faab_bids_scale_with_the_lineup_improvement(self):
        self.cfg["waivers"]["type"] = "faab"
        self.cfg["waivers"]["faab_budget"] = 100
        big = waivers.faab_bid(self.cfg, gain_week=10.0, lineup_now=100.0, remaining=100)
        small = waivers.faab_bid(self.cfg, gain_week=1.0, lineup_now=100.0, remaining=100)
        self.assertGreater(big, small)
        self.assertEqual(waivers.faab_bid(self.cfg, 0.0, 100.0, 100), 0)
        # Never everything, however good he looks.
        self.assertLessEqual(waivers.faab_bid(self.cfg, 500.0, 100.0, 100), 60)

    def test_faab_respects_what_is_actually_left(self):
        self.cfg["waivers"]["type"] = "faab"
        self.assertLess(
            waivers.faab_bid(self.cfg, 5.0, 100.0, remaining=10),
            waivers.faab_bid(self.cfg, 5.0, 100.0, remaining=100),
        )

    def test_drop_candidates_are_cheapest_first(self):
        rows = waivers.drop_candidates(self.cfg, self.roster)
        costs = [r["cost"] for r in rows]
        self.assertEqual(costs, sorted(costs))
        self.assertFalse(rows[0]["starter"])

    def test_free_agents_exclude_everyone_rostered(self):
        pool = self.roster + [player("Free Guy", "WR", 9)]
        rosters = {"team-1": self.roster[:5], "team-2": self.roster[5:]}
        free = waivers.free_agents(pool, rosters)
        self.assertEqual([p.name for p in free], ["Free Guy"])

    def test_acquisition_limits_are_reported(self):
        self.cfg["waivers"]["max_acquisitions_week"] = 2
        self.assertEqual(waivers.acquisition_limits(self.cfg, added_this_week=1), [])
        self.assertTrue(waivers.acquisition_limits(self.cfg, added_this_week=2))

    def test_a_capped_search_says_so(self):
        extras = [player(f"Guy {i}", "WR", 5 + i * 0.01) for i in range(40)]
        result = waivers.recommend(
            self.cfg, self.roster, extras, limit=3, search_width=10
        )
        self.assertEqual(result["considered"], 10)
        self.assertEqual(result["truncated"], 30)


# --------------------------------------------------------------------------
# Matchups
# --------------------------------------------------------------------------

class TestSchedule(unittest.TestCase):
    def test_every_team_plays_once_a_week(self):
        for size in (8, 10, 12):
            cfg = league(size)
            for entry in matchups.schedule(cfg):
                seen = [t for game in entry["games"] for t in (game["home"], game["away"])]
                self.assertEqual(len(seen), size, f"{size} teams, week {entry['week']}")
                self.assertEqual(len(set(seen)), size)

    def test_nobody_plays_themselves(self):
        for entry in matchups.schedule(league(12)):
            for game in entry["games"]:
                self.assertNotEqual(game["home"], game["away"])

    def test_an_odd_league_gives_one_team_a_bye(self):
        cfg = league(9)
        for entry in matchups.schedule(cfg):
            self.assertEqual(len(entry["games"]), 4)

    def test_a_full_round_robin_before_anyone_repeats(self):
        cfg = league(10)
        me = cfg["teams"][0]["id"]
        opponents = [matchups.opponent_of(cfg, me, week) for week in range(1, 10)]
        self.assertEqual(len(set(opponents)), 9)
        self.assertNotIn(me, opponents)

    def test_the_schedule_is_deterministic(self):
        self.assertEqual(matchups.schedule(league(10)), matchups.schedule(league(10)))

    def test_a_configured_schedule_wins(self):
        cfg = league(4)
        ids = [t["id"] for t in cfg["teams"]]
        cfg["schedule"] = [
            {"week": 1, "games": [{"home": ids[0], "away": ids[3]}]},
        ]
        got = matchups.schedule(cfg)
        self.assertEqual(len(got), 1)
        self.assertEqual(matchups.opponent_of(cfg, ids[0], 1), ids[3])

    def test_a_configured_schedule_drops_unknown_teams(self):
        cfg = league(4)
        cfg["schedule"] = [
            {"week": 1, "games": [{"home": "ghost", "away": cfg["teams"][0]["id"]}]},
        ]
        self.assertEqual(matchups.schedule(cfg)[0]["games"], [])


class TestResults(unittest.TestCase):
    def setUp(self):
        self.cfg = league(4)
        self.ids = [t["id"] for t in self.cfg["teams"]]
        a, b, c, d = self.ids
        self.cfg["schedule"] = [
            {"week": 1, "games": [{"home": a, "away": b}, {"home": c, "away": d}]},
            {"week": 2, "games": [{"home": a, "away": c}, {"home": b, "away": d}]},
        ]
        # Everyone finishes 1-1, so points-for is the only thing separating them.
        self.scores = {
            1: {a: 120.0, b: 100.0, c: 90.0, d: 95.0},
            2: {a: 110.0, b: 140.0, c: 111.0, d: 105.0},
        }

    def test_week_summary_calls_the_winner(self):
        summary = matchups.week_summary(self.cfg, 1, self.scores)
        first = summary["games"][0]
        self.assertEqual(first["outcome"], "home")
        self.assertEqual(first["margin"], 20.0)
        self.assertTrue(summary["complete"])

    def test_an_unscored_week_is_pending_not_a_loss(self):
        summary = matchups.week_summary(self.cfg, 2, {1: self.scores[1]})
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["games"][0]["outcome"], "pending")
        self.assertIsNone(summary["games"][0]["margin"])

    def test_my_game_is_told_from_my_side(self):
        summary = matchups.week_summary(self.cfg, 1, self.scores, my_team_id=self.ids[1])
        game = summary["my_game"]
        self.assertEqual(game["result"], "loss")
        self.assertEqual(game["my_points"], 100.0)
        self.assertEqual(game["opponent"], self.ids[0])

    def test_standings_count_records_and_points(self):
        rows = {r["team_id"]: r for r in matchups.standings(self.cfg, self.scores)}
        a, b, c, d = self.ids
        self.assertEqual(rows[a]["record"], "1-1")
        self.assertEqual(rows[a]["points_for"], 230.0)
        self.assertEqual(rows[a]["points_against"], 211.0)
        self.assertEqual(rows[b]["record"], "1-1")
        self.assertEqual(rows[c]["record"], "1-1")
        self.assertEqual(rows[d]["record"], "1-1")

    def test_a_tie_is_a_tie(self):
        a, b = self.ids[0], self.ids[1]
        rows = {r["team_id"]: r for r in
                matchups.standings(self.cfg, {1: {a: 100.0, b: 100.0}})}
        self.assertEqual(rows[a]["ties"], 1)
        self.assertEqual(rows[a]["record"], "0-0-1")

    def test_streaks(self):
        rows = {r["team_id"]: r for r in matchups.standings(self.cfg, self.scores)}
        self.assertEqual(rows[self.ids[1]]["streak"], "W1")   # lost, then won
        self.assertEqual(rows[self.ids[0]]["streak"], "L1")

    def test_the_playoff_line_comes_from_the_config(self):
        self.cfg["playoffs"]["teams"] = 2
        rows = matchups.standings(self.cfg, self.scores)
        self.assertEqual([r["in_playoffs"] for r in rows], [True, True, False, False])

    def test_ties_break_on_points_for(self):
        rows = matchups.standings(self.cfg, self.scores)
        self.assertTrue(all(r["record"] == "1-1" for r in rows))
        self.assertEqual([r["team_id"] for r in rows],
                         [self.ids[1], self.ids[0], self.ids[2], self.ids[3]])

    def test_play_against_median_adds_a_second_result(self):
        self.cfg["settings_misc"]["play_against_median"] = True
        rows = {r["team_id"]: r for r in matchups.standings(self.cfg, self.scores)}
        # Head to head is 1-1 for everyone; the median result is what separates.
        self.assertEqual(rows[self.ids[0]]["total_wins"] + rows[self.ids[0]]["total_losses"], 4)
        self.assertTrue(any(r["median_wins"] for r in rows.values()))

    def test_median_of_a_week(self):
        self.assertEqual(matchups.median_score({"a": 10.0, "b": 20.0}), 15.0)
        self.assertEqual(matchups.median_score({"a": 10.0, "b": 20.0, "c": 12.0}), 12.0)
        self.assertIsNone(matchups.median_score({}))

    def test_default_week_is_the_first_unscored_one(self):
        self.assertEqual(matchups.default_week(self.cfg, {}), 1)
        self.assertEqual(matchups.default_week(self.cfg, self.scores), 3)

    def test_projections_fill_in_before_kickoff(self):
        rosters = {self.ids[0]: [player("RB A", "RB", 20), player("QB A", "QB", 18)]}
        projected = matchups.project_week(self.cfg, 1, rosters)
        summary = matchups.week_summary(self.cfg, 1, {}, projected, self.ids[0])
        self.assertEqual(summary["my_game"]["result"], "pending")
        self.assertEqual(summary["my_game"]["my_projected"], 38.0)


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------

class TestTransactions(unittest.TestCase):
    def setUp(self):
        self.cfg = league(4)
        self.a, self.b = self.cfg["teams"][0]["id"], self.cfg["teams"][1]["id"]
        self.base = {self.a: ["p1", "p2"], self.b: ["p3", "p4"]}

    def txn(self, **kw):
        kw.setdefault("team_id", self.a)
        return T.Transaction(**kw)

    def test_add_and_drop_move_one_roster(self):
        after = T.apply_to_rosters(
            self.base,
            [self.txn(type="add_drop", adds=["p9"], drops=["p1"], id=1)],
        )
        self.assertEqual(after[self.a], ["p2", "p9"])
        self.assertEqual(after[self.b], ["p3", "p4"])

    def test_a_trade_moves_both(self):
        after = T.apply_to_rosters(self.base, [self.txn(
            type="trade", partner_team_id=self.b, adds=["p3"], drops=["p1"], id=1)])
        self.assertEqual(sorted(after[self.a]), ["p2", "p3"])
        self.assertEqual(sorted(after[self.b]), ["p1", "p4"])

    def test_moves_replay_in_order(self):
        after = T.apply_to_rosters(self.base, [
            self.txn(type="add", adds=["p9"], id=1),
            self.txn(type="drop", drops=["p9"], id=2),
        ])
        self.assertNotIn("p9", after[self.a])

    def test_replaying_is_idempotent_on_the_base(self):
        """Deriving rosters must never mutate the snapshot it derives from."""
        T.apply_to_rosters(self.base, [self.txn(type="drop", drops=["p1"], id=1)])
        self.assertEqual(self.base[self.a], ["p1", "p2"])

    def test_dropping_someone_you_do_not_have_is_harmless(self):
        after = T.apply_to_rosters(self.base, [self.txn(type="drop", drops=["ghost"], id=1)])
        self.assertEqual(after[self.a], ["p1", "p2"])

    def test_adding_someone_twice_does_not_duplicate(self):
        after = T.apply_to_rosters(self.base, [
            self.txn(type="add", adds=["p9"], id=1),
            self.txn(type="add", adds=["p9"], id=2),
        ])
        self.assertEqual(after[self.a].count("p9"), 1)

    def test_validation_catches_the_usual_mistakes(self):
        self.assertTrue(T.validate(self.txn(type="add", team_id="ghost", adds=["p9"]), self.cfg))
        self.assertTrue(T.validate(self.txn(type="trade", adds=["p9"]), self.cfg))
        self.assertTrue(T.validate(
            self.txn(type="trade", partner_team_id=self.a, adds=["p9"]), self.cfg))
        self.assertTrue(T.validate(self.txn(type="add"), self.cfg))
        self.assertEqual(T.validate(self.txn(type="add", adds=["p9"]), self.cfg), [])

    def test_a_bid_over_budget_is_rejected(self):
        self.cfg["waivers"]["faab_budget"] = 100
        self.assertTrue(T.validate(self.txn(type="add", adds=["p9"], faab=150), self.cfg))
        self.assertEqual(T.validate(self.txn(type="add", adds=["p9"], faab=99), self.cfg), [])

    def test_round_trip_through_a_dict(self):
        original = self.txn(type="add_drop", adds=["p9"], drops=["p1"],
                            week=4, faab=12.5, id=3)
        self.assertEqual(T.Transaction.from_dict(original.to_dict()), original)

    def test_activity_summarises_each_team(self):
        players = {
            "p9": player("New Guy", "RB", 12),
            "p1": player("Old Guy", "RB", 4),
        }
        report = T.activity(self.cfg, [
            self.txn(type="add_drop", adds=["p9"], drops=["p1"], faab=20, week=2, id=1),
        ], players)
        mine = next(r for r in report["teams"] if r["team_id"] == self.a)
        self.assertEqual(mine["adds"], 1)
        self.assertEqual(mine["faab_spent"], 20.0)
        self.assertEqual(mine["net_value"], 8.0)          # 12 in, 4 out
        self.assertEqual(report["positions_chased"], [{"pos": "RB", "adds": 1}])

    def test_activity_credits_both_sides_of_a_trade(self):
        players = {"p1": player("Mine", "RB", 10), "p3": player("Theirs", "WR", 14)}
        report = T.activity(self.cfg, [self.txn(
            type="trade", partner_team_id=self.b, adds=["p3"], drops=["p1"], id=1)], players)
        rows = {r["team_id"]: r for r in report["teams"]}
        self.assertEqual(rows[self.a]["trades"], 1)
        self.assertEqual(rows[self.b]["trades"], 1)
        self.assertEqual(rows[self.a]["net_value"], 4.0)
        self.assertEqual(rows[self.b]["net_value"], -4.0)

    def test_the_feed_reads_newest_first(self):
        players = {"p9": player("New Guy", "RB", 12), "p1": player("Old Guy", "RB", 4)}
        report = T.activity(self.cfg, [
            self.txn(type="add", adds=["p9"], week=1, id=1),
            self.txn(type="drop", drops=["p1"], week=2, id=2),
        ], players)
        self.assertEqual([f["id"] for f in report["feed"]], [2, 1])
        self.assertIn("dropped Old Guy", report["feed"][0]["summary"])

    def test_activity_can_start_from_a_week(self):
        report = T.activity(self.cfg, [
            self.txn(type="add", adds=["p9"], week=1, id=1),
            self.txn(type="add", adds=["p8"], week=5, id=2),
        ], {}, since_week=5)
        self.assertEqual(report["total"], 1)

    def test_acquisition_counting(self):
        rows = [
            self.txn(type="add", adds=["p9"], week=3, id=1),
            self.txn(type="add_drop", adds=["p8"], drops=["p1"], week=3, id=2),
            self.txn(type="add", adds=["p7"], week=4, id=3),
            self.txn(type="trade", partner_team_id=self.b, adds=["p3"], week=4, id=4),
        ]
        self.assertEqual(T.acquisitions(rows, self.a, week=3), 2)
        self.assertEqual(T.acquisitions(rows, self.a), 3)   # the trade does not count
        self.assertEqual(T.acquisitions(rows, self.b), 0)


if __name__ == "__main__":
    unittest.main()
