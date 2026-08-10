"""Tests for the pieces where a bug would quietly cost you a draft."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from ff import config as C
from ff import trades
from ff.draft import DraftState, build_slots, recommend
from ff.players import Player, make_player_id, normalize_name, parse_projection_csv
from ff.roster import need_multiplier, optimal_lineup, unfilled_slots
from ff.scoring import band_points, score_stats
from ff.valuation import compute_values, depth_warnings, replacement_levels


def league(**overrides):
    cfg = C.new_config("Test League", "manual", 12, 0.5)
    cfg["teams"] = [C.new_team(f"Team {i}") for i in range(1, 13)]
    cfg["my_team_id"] = cfg["teams"][0]["id"]
    cfg.update(overrides)
    return cfg


def player(name, pos, points, **kw):
    p = Player(player_id=make_player_id(name, pos, ""), name=name, pos=pos, **kw)
    p.points = points
    p.vor = kw.get("vor", points)
    return p


class TestScoring(unittest.TestCase):
    def test_ppr_setting_changes_the_answer(self):
        stats = {"rec": 100, "rec_yd": 1200, "rec_td": 8}
        half = score_stats(stats, {"scoring": C.default_scoring(0.5)})
        full = score_stats(stats, {"scoring": C.default_scoring(1.0)})
        standard = score_stats(stats, {"scoring": C.default_scoring(0.0)})
        self.assertEqual(full - half, 50)
        self.assertEqual(half - standard, 50)

    def test_unknown_stats_are_ignored_not_fatal(self):
        points = score_stats({"rush_yd": 100, "made_up_stat": 999}, {"scoring": {"rush_yd": 0.1}})
        self.assertEqual(points, 10.0)

    def test_bonus_threshold(self):
        cfg = {
            "scoring": {"pass_yd": 0.04},
            "scoring_bonuses": [{"stat": "pass_yd", "threshold": 300, "points": 3}],
        }
        self.assertEqual(score_stats({"pass_yd": 299}, cfg), round(299 * 0.04, 2))
        self.assertEqual(score_stats({"pass_yd": 300}, cfg), round(300 * 0.04 + 3, 2))

    def test_negative_scoring_applies(self):
        cfg = {"scoring": {"pass_int": -2}}
        self.assertEqual(score_stats({"pass_int": 3}, cfg), -6.0)


class TestConfig(unittest.TestCase):
    def test_default_config_is_valid(self):
        self.assertEqual(C.validate(league()), [])

    def test_team_count_mismatch_is_caught(self):
        cfg = league()
        cfg["teams"] = cfg["teams"][:8]
        self.assertTrue(any("team count" in p for p in C.validate(cfg)))

    def test_flex_is_spread_across_eligible_positions(self):
        counts = C.starters_by_position(league())
        self.assertAlmostEqual(counts["RB"], 2.45, places=2)
        self.assertAlmostEqual(counts["WR"], 2.45, places=2)
        self.assertAlmostEqual(counts["TE"], 1.10, places=2)
        self.assertAlmostEqual(counts["QB"], 1.0, places=2)

    def test_superflex_lifts_qb_demand(self):
        cfg = league()
        cfg["roster"]["slots"].append(
            {"slot": "SUPERFLEX", "count": 1, "eligible": ["QB", "RB", "WR", "TE"]}
        )
        cfg["valuation"]["flex_share"] = {"QB": 0.7, "RB": 0.1, "WR": 0.1, "TE": 0.1}
        self.assertGreater(C.starters_by_position(cfg)["QB"], 1.0)

    def test_migrate_fills_missing_blocks(self):
        migrated = C.migrate({"name": "Old League", "scoring": {"rush_td": 6}})
        self.assertIn("trades", migrated)
        self.assertIn("max_value_gap_pct", migrated["trades"]["fairness"])
        self.assertEqual(migrated["scoring"], {"rush_td": 6})

    def test_unknown_scoring_stat_is_rejected(self):
        cfg = league()
        cfg["scoring"]["not_a_real_stat"] = 5
        self.assertTrue(any("Unknown scoring stat" in p for p in C.validate(cfg)))


class TestDraftSchedule(unittest.TestCase):
    def test_countdown_matches_a_known_draft_time(self):
        cfg = league()
        cfg["draft"]["scheduled_at"] = "2026-08-30T17:45:00-04:00"
        cfg["draft"]["timezone"] = "EDT"
        cfg["draft"]["arrive_minutes_early"] = 10
        now = datetime(2026, 8, 10, 10, 7, tzinfo=timezone(timedelta(hours=-4)))

        schedule = C.draft_schedule(cfg, now)
        self.assertTrue(schedule["scheduled"])
        self.assertEqual(schedule["countdown"], "20 days, 7 hours, 38 minutes")
        self.assertIn("Sunday", schedule["label"])
        self.assertFalse(schedule["past"])
        self.assertTrue(schedule["arrive_by"].endswith("17:35:00-04:00"))

    def test_unscheduled_draft_is_reported_not_faked(self):
        self.assertEqual(C.draft_schedule(league()), {"scheduled": False})

    def test_a_draft_in_the_past_is_marked(self):
        cfg = league()
        cfg["draft"]["scheduled_at"] = "2020-09-01T20:00:00-04:00"
        now = datetime(2026, 8, 10, tzinfo=timezone(timedelta(hours=-4)))
        schedule = C.draft_schedule(cfg, now)
        self.assertTrue(schedule["past"])
        self.assertEqual(schedule["countdown"], "underway")

    def test_unreadable_draft_time_fails_validation(self):
        cfg = league()
        cfg["draft"]["scheduled_at"] = "next Tuesday-ish"
        self.assertTrue(any("Draft date/time" in p for p in C.validate(cfg)))

    def test_timezone_offset_is_respected(self):
        """The same wall-clock time in two zones is not the same instant."""
        east, west = league(), league()
        east["draft"]["scheduled_at"] = "2026-08-30T17:45:00-04:00"
        west["draft"]["scheduled_at"] = "2026-08-30T17:45:00-07:00"
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        gap = (C.draft_schedule(west, now)["seconds_until"]
               - C.draft_schedule(east, now)["seconds_until"])
        self.assertEqual(gap, 3 * 3600)


class TestScoringBands(unittest.TestCase):
    PA_BANDS = [
        {"max": 0, "points": 10}, {"max": 6, "points": 7}, {"max": 13, "points": 4},
        {"max": 20, "points": 2}, {"max": 27, "points": 0}, {"max": 34, "points": -1},
        {"max": None, "points": -4},
    ]

    def test_each_band_returns_its_own_value(self):
        expected = [(0, 10), (6, 7), (7, 4), (13, 4), (17, 2), (24, 0), (30, -1), (45, -4)]
        for allowed, points in expected:
            self.assertEqual(band_points(allowed, self.PA_BANDS), points, f"PA={allowed}")

    def test_banded_stat_is_looked_up_not_multiplied(self):
        cfg = {"scoring": {"dst_sack": 1}, "scoring_bands": {"dst_pa": self.PA_BANDS}}
        # 10 points allowed must score the band value (4), not 10 x anything.
        self.assertEqual(score_stats({"dst_sack": 3, "dst_pa": 10}, cfg), 7.0)

    def test_bands_must_end_open_ended(self):
        cfg = league()
        cfg["scoring_bands"] = {"dst_pa": [{"max": 20, "points": 2}]}
        self.assertTrue(any("open-ended" in p for p in C.validate(cfg)))

    def test_valid_bands_pass_validation(self):
        cfg = league()
        cfg["scoring_bands"] = {"dst_pa": self.PA_BANDS}
        self.assertEqual(C.validate(cfg), [])


class TestRealLeagueConfig(unittest.TestCase):
    """The Keg South config is a real league; its unusual rules are a good
    regression target for the whole scoring pipeline."""

    def setUp(self):
        self.cfg = C.load("keg-south")

    def test_it_is_valid(self):
        self.assertEqual(C.validate(self.cfg), [])

    def test_yardage_bonuses_stack(self):
        # 400 passing yards earns the 300 bonus (+3) and the 400 bonus (+4).
        base = score_stats({"pass_yd": 400}, self.cfg)
        self.assertAlmostEqual(base, 400 / 50 + 3 + 4, places=2)

    def test_six_point_passing_touchdowns(self):
        self.assertEqual(score_stats({"pass_td": 1}, self.cfg), 6.0)

    def test_full_ppr(self):
        self.assertEqual(score_stats({"rec": 10}, self.cfg), 10.0)

    def test_defense_points_allowed_band(self):
        self.assertEqual(score_stats({"dst_pa": 0}, self.cfg), 10.0)
        self.assertEqual(score_stats({"dst_pa": 40}, self.cfg), -4.0)

    def test_roster_is_nine_starters_and_six_bench(self):
        starters = sum(s["count"] for s in self.cfg["roster"]["slots"])
        self.assertEqual(starters, 9)
        self.assertEqual(self.cfg["roster"]["bench"], 6)
        self.assertEqual(C.roster_capacity(self.cfg), self.cfg["draft"]["rounds"])

    def test_ten_teams_with_unique_ids(self):
        ids = [t["id"] for t in self.cfg["teams"]]
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(set(ids)), 10)
        self.assertTrue(all(ids), "every team needs a non-empty id")


class TestPaths(unittest.TestCase):
    """Data locations must be overridable, so two leagues (and later, two
    users) can run against separate data without colliding."""

    def test_env_override_redirects_every_write_path(self):
        import importlib

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "FF_DATA_DIR": os.path.join(tmp, "data"),
                "FF_LEAGUES_DIR": os.path.join(tmp, "leagues"),
                "FF_SECRETS_DIR": os.path.join(tmp, "secrets"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                paths = importlib.reload(importlib.import_module("ff.paths"))
                self.assertTrue(str(paths.LEAGUES_DIR).startswith(tmp))
                self.assertTrue(str(paths.DATA_DIR).startswith(tmp))
                self.assertTrue(str(paths.DB_PATH).startswith(tmp))
                # Projections default *under* the overridden data dir.
                self.assertTrue(str(paths.PROJECTIONS_DIR).startswith(tmp))
                self.assertTrue(paths.describe()["custom"])

        # Restore the module so later tests see the real directories.
        importlib.reload(importlib.import_module("ff.paths"))

    def test_defaults_live_in_the_repo(self):
        import ff.paths as paths

        self.assertEqual(paths.LEAGUES_DIR, paths.ROOT / "leagues")
        self.assertEqual(paths.DB_PATH, paths.ROOT / "data" / "fantasy.db")


class TestProjectionImport(unittest.TestCase):
    def test_common_export_headers_are_mapped(self):
        csv = (
            "Player,Team,POS,Bye,Rush Yds,Rush TD,REC,Rec Yds,Rec TD\n"
            "Bijan Robinson,ATL,RB,5,1350,11,62,520,3\n"
        )
        rows = parse_projection_csv(csv)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].stats["rush_yd"], 1350)
        self.assertEqual(rows[0].stats["rec"], 62)
        self.assertEqual(rows[0].pos, "RB")
        self.assertEqual(rows[0].bye, 5)

    def test_title_row_above_the_header_is_skipped(self):
        csv = "2025 Projections - Standard\nPlayer,Pos,Rec Yds\nSome Guy,WR,1100\n"
        rows = parse_projection_csv(csv)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Some Guy")

    def test_team_suffix_in_name_is_split_out(self):
        rows = parse_projection_csv("Player,Pos,Rec Yds\nJa'Marr Chase CIN,WR,1520\n")
        self.assertEqual(rows[0].name, "Ja'Marr Chase")
        self.assertEqual(rows[0].team, "CIN")

    def test_defense_position_aliases(self):
        rows = parse_projection_csv("Player,Pos,Rec Yds\nDenver,D/ST,0\n")
        self.assertEqual(rows[0].pos, "DST")

    def test_name_normalisation_survives_punctuation(self):
        self.assertEqual(normalize_name("A.J. Brown"), normalize_name("AJ Brown"))
        self.assertEqual(normalize_name("Brian Robinson Jr."), "brian robinson")


class TestValuation(unittest.TestCase):
    def setUp(self):
        self.cfg = league()
        self.pool = (
            [player(f"QB{i}", "QB", 400 - i * 8) for i in range(1, 25)]
            + [player(f"RB{i}", "RB", 300 - i * 5) for i in range(1, 61)]
            + [player(f"WR{i}", "WR", 290 - i * 4) for i in range(1, 61)]
            + [player(f"TE{i}", "TE", 200 - i * 6) for i in range(1, 25)]
        )
        compute_values(self.pool, self.cfg)

    def test_replacement_tracks_league_size(self):
        levels = replacement_levels(self.pool, self.cfg)
        # 12 teams x 1 QB => the 12th QB is replacement level.
        self.assertEqual(levels["QB"], 400 - 12 * 8)

    def test_vor_makes_positions_comparable(self):
        qb1 = next(p for p in self.pool if p.name == "QB1")
        rb1 = next(p for p in self.pool if p.name == "RB1")
        # QB1 outscores RB1 by 97 raw points but is worth less over replacement.
        self.assertGreater(qb1.points, rb1.points)
        self.assertGreater(rb1.vor, qb1.vor)

    def test_tiers_start_at_one_and_increase(self):
        rbs = sorted((p for p in self.pool if p.pos == "RB"), key=lambda p: p.pos_rank)
        self.assertEqual(rbs[0].value_tier, 1)
        self.assertTrue(all(b.value_tier >= a.value_tier for a, b in zip(rbs, rbs[1:])))

    def test_empty_position_does_not_crash(self):
        levels = replacement_levels([], self.cfg)
        self.assertEqual(levels["RB"], 0.0)

    def test_shallow_pool_is_reported_not_silently_clamped(self):
        """A 12-team league needs the 12th QB as its baseline. Give it 6 and
        the baseline clamps to the worst one, overstating every QB — which the
        numbers alone would never reveal."""
        shallow = [player(f"QB{i}", "QB", 400 - i * 8) for i in range(1, 7)]
        warnings = depth_warnings(shallow, self.cfg)
        qb = next((w for w in warnings if w["pos"] == "QB"), None)
        self.assertIsNotNone(qb)
        self.assertEqual(qb["have"], 6)
        self.assertEqual(qb["needed"], 12)

    def test_deep_pool_produces_no_warning(self):
        self.assertEqual(
            [w["pos"] for w in depth_warnings(self.pool, self.cfg) if w["pos"] == "QB"],
            [],
        )


class TestRoster(unittest.TestCase):
    def test_flex_takes_the_best_leftover(self):
        cfg = league()
        roster = [
            player("QB", "QB", 300), player("RB1", "RB", 250), player("RB2", "RB", 200),
            player("WR1", "WR", 240), player("WR2", "WR", 220), player("TE1", "TE", 150),
            player("WR3", "WR", 210), player("K1", "K", 130), player("D1", "DST", 120),
        ]
        lineup, total = optimal_lineup(roster, cfg)
        self.assertEqual(lineup["FLEX"][0].name, "WR3")
        self.assertEqual(total, sum(p.points for p in roster))

    def test_unfilled_slots_are_reported(self):
        gaps = unfilled_slots([player("QB", "QB", 300)], league())
        self.assertEqual(gaps["RB"], 2)
        self.assertNotIn("QB", gaps)

    def test_need_falls_once_a_position_is_stocked(self):
        cfg = league()
        empty = need_multiplier([], "RB", cfg)
        stocked = need_multiplier(
            [player(f"RB{i}", "RB", 200) for i in range(5)], "RB", cfg
        )
        self.assertGreater(empty, 1.0)
        self.assertLess(stocked, empty)

    def test_need_weight_zero_disables_the_adjustment(self):
        cfg = league()
        cfg["valuation"]["need_weight"] = 0
        self.assertEqual(need_multiplier([], "RB", cfg), 1.0)


class TestDraft(unittest.TestCase):
    def test_snake_order_reverses_every_round(self):
        cfg = league()
        slots = build_slots(cfg)
        self.assertEqual(len(slots), 12 * cfg["draft"]["rounds"])
        first = [s.team_id for s in slots[:12]]
        second = [s.team_id for s in slots[12:24]]
        self.assertEqual(first, list(reversed(second)))

    def test_linear_order_repeats(self):
        cfg = league()
        cfg["draft"]["type"] = "linear"
        slots = build_slots(cfg)
        self.assertEqual([s.team_id for s in slots[:12]], [s.team_id for s in slots[12:24]])

    def test_pick_and_undo_round_trip(self):
        cfg = league()
        state = DraftState(cfg)
        target = player("Bijan Robinson", "RB", 300)
        state.make_pick(target)
        self.assertEqual(len(state.made), 1)
        self.assertIn(target.player_id, state.drafted_ids())
        undone = state.undo()
        self.assertEqual(len(state.made), 0)
        self.assertNotIn(target.player_id, state.drafted_ids())
        # Regression: undo used to blank the pick before returning it, so the
        # UI reported "undid: " with no name.
        self.assertEqual(undone.player_name, "Bijan Robinson")
        self.assertEqual(undone.pos, "RB")

    def test_undo_on_an_empty_draft_is_harmless(self):
        self.assertIsNone(DraftState(league()).undo())

    def test_double_drafting_is_refused(self):
        state = DraftState(league())
        target = player("Bijan Robinson", "RB", 300)
        state.make_pick(target)
        with self.assertRaises(ValueError):
            state.make_pick(target)

    def test_picks_until_next_counts_the_snake_turn(self):
        cfg = league()
        state = DraftState(cfg)
        first_team = state.slots[0].team_id
        self.assertEqual(state.picks_until_next(first_team), 0)
        state.make_pick(player("Someone", "RB", 300))
        # Team 1 picks 1st and 24th, so 22 picks happen in between.
        self.assertEqual(state.picks_until_next(first_team), 22)

    def test_recommendations_prefer_starters_over_holes_not_kickers(self):
        cfg = league()
        pool = compute_values(
            [player(f"RB{i}", "RB", 300 - i * 5) for i in range(1, 61)]
            + [player(f"WR{i}", "WR", 290 - i * 4) for i in range(1, 61)]
            + [player(f"K{i}", "K", 140 - i) for i in range(1, 25)]
            + [player(f"QB{i}", "QB", 400 - i * 8) for i in range(1, 25)],
            cfg,
        )
        state = DraftState(cfg)
        team = cfg["teams"][0]["id"]
        top = recommend(state, pool, team, limit=5)
        self.assertTrue(top)
        # A kicker must never be the top recommendation with an empty roster.
        self.assertNotEqual(top[0]["player"]["pos"], "K")

    def test_recommendation_gain_is_bounded_by_vor(self):
        """Regression: an empty slot once counted as zero, so any hole-filling
        pick scored as if it added its entire projection."""
        cfg = league()
        pool = compute_values(
            [player(f"QB{i}", "QB", 400 - i * 8) for i in range(1, 25)]
            + [player(f"RB{i}", "RB", 300 - i * 5) for i in range(1, 61)],
            cfg,
        )
        state = DraftState(cfg)
        top = recommend(state, pool, cfg["teams"][0]["id"], limit=40)
        for row in top:
            self.assertLessEqual(row["starter_gain"], row["player"]["vor"] + 0.01)


class TestTrades(unittest.TestCase):
    def setUp(self):
        self.cfg = league()
        # Team A is stacked at RB and thin at WR; Team B is the mirror image.
        #
        # A carries FOUR startable RBs against 2 RB slots + 1 FLEX, so "A RB4"
        # genuinely rides the bench and is worth nothing to the lineup — that
        # is what makes him tradeable surplus. A's weak spot is "A WR2", who
        # starts only because somebody has to.
        self.a = [
            player("A QB", "QB", 300, vor=40), player("A RB1", "RB", 280, vor=120),
            player("A RB2", "RB", 260, vor=100), player("A RB3", "RB", 245, vor=85),
            player("A RB4", "RB", 240, vor=80),
            player("A WR1", "WR", 180, vor=20), player("A WR2", "WR", 150, vor=-10),
            player("A TE", "TE", 150, vor=40), player("A K", "K", 130, vor=10),
            player("A DST", "DST", 120, vor=10),
        ]
        # B has three startable WRs for 2 WR slots and a barren backfield.
        self.b = [
            player("B QB", "QB", 290, vor=30), player("B RB1", "RB", 200, vor=40),
            player("B RB2", "RB", 170, vor=10), player("B WR1", "WR", 270, vor=110),
            player("B WR2", "WR", 250, vor=90), player("B WR3", "WR", 230, vor=70),
            player("B TE", "TE", 145, vor=35), player("B K", "K", 128, vor=8),
            player("B DST", "DST", 118, vor=8),
        ]

    def test_swapping_surplus_for_need_helps_both_sides(self):
        """A's benched RB4 is worth nothing to his lineup; B's WR3 is worth
        nothing to hers. Swapping them upgrades both starting lineups."""
        result = trades.evaluate(
            self.cfg, self.a, self.b,
            [p for p in self.a if p.name == "A RB4"],
            [p for p in self.b if p.name == "B WR3"],
        )
        self.assertTrue(result["legal"])
        self.assertEqual(result["verdict"], "win-win")
        self.assertGreater(result["team_a"]["lineup_gain"], 0)
        self.assertGreater(result["team_b"]["lineup_gain"], 0)

    def test_trading_a_starter_straight_across_is_not_a_win(self):
        """Regression: with no bench depth every player starts, so a swap that
        loses value must not be dressed up as a win-win."""
        result = trades.evaluate(
            self.cfg, self.a, self.b,
            [p for p in self.a if p.name == "A RB1"],
            [p for p in self.b if p.name == "B RB2"],
        )
        self.assertLess(result["team_a"]["lineup_gain"], 0)
        self.assertNotEqual(result["verdict"], "win-win")

    def test_deadline_makes_a_trade_illegal(self):
        self.cfg["trades"]["deadline_week"] = 11
        result = trades.evaluate(
            self.cfg, self.a, self.b,
            [self.a[3]], [self.b[5]], week=13,
        )
        self.assertFalse(result["legal"])
        self.assertTrue(any("deadline" in v for v in result["violations"]))

    def test_package_size_limit_is_enforced(self):
        self.cfg["trades"]["max_players_per_side"] = 1
        result = trades.evaluate(
            self.cfg, self.a, self.b, self.a[1:3], [self.b[3]],
        )
        self.assertFalse(result["legal"])

    def test_uneven_trades_can_be_banned(self):
        self.cfg["trades"]["allow_uneven"] = False
        result = trades.evaluate(self.cfg, self.a, self.b, self.a[1:3], [self.b[3]])
        self.assertFalse(result["legal"])
        self.assertTrue(any("uneven" in v for v in result["violations"]))

    def test_untouchable_position_blocks_the_deal(self):
        self.cfg["trades"]["untouchable_positions"] = ["QB"]
        result = trades.evaluate(self.cfg, self.a, self.b, [self.a[0]], [self.b[3]])
        self.assertFalse(result["legal"])

    def test_suggestions_respect_league_rules(self):
        self.cfg["trades"]["max_players_per_side"] = 1
        found = trades.suggest(self.cfg, self.a, {"b": self.b}, limit=5)
        for deal in found:
            self.assertEqual(len(deal["team_a"]["sends"]), 1)
            self.assertEqual(len(deal["team_a"]["receives"]), 1)
            self.assertGreater(deal["team_a"]["lineup_gain"], 0)
            self.assertGreater(deal["team_b"]["lineup_gain"], 0)

    def test_suggestions_find_the_obvious_rb_for_wr_swap(self):
        found = trades.suggest(self.cfg, self.a, {"b": self.b}, limit=10)
        self.assertTrue(found, "expected at least one mutually beneficial trade")
        top = found[0]
        self.assertEqual(top["partner_team_id"], "b")
        self.assertTrue(any(p["pos"] == "WR" for p in top["team_a"]["receives"]))

    def test_veto_risk_is_flagged_and_sorted_last(self):
        """A legal but lopsided deal must be marked, and must rank below a
        cleaner deal even if it gains slightly more."""
        result = trades.evaluate(
            self.cfg, self.a, self.b,
            [p for p in self.a if p.name == "A RB4"],
            [p for p in self.b if p.name == "B WR1"],
        )
        self.assertTrue(result["veto_risk"])

        clean = trades.evaluate(
            self.cfg, self.a, self.b,
            [p for p in self.a if p.name == "A RB4"],
            [p for p in self.b if p.name == "B WR3"],
        )
        self.assertFalse(clean["veto_risk"])

        found = trades.suggest(self.cfg, self.a, {"b": self.b}, limit=20)
        risky = [i for i, d in enumerate(found) if d["veto_risk"]]
        safe = [i for i, d in enumerate(found) if not d["veto_risk"]]
        if risky and safe:
            self.assertLess(max(safe), min(risky))

    def test_lopsided_trade_is_flagged_not_recommended(self):
        result = trades.evaluate(
            self.cfg, self.a, self.b,
            [p for p in self.a if p.name == "A WR2"],
            [p for p in self.b if p.name == "B WR1"],
        )
        self.assertNotEqual(result["verdict"], "win-win")
        self.assertGreater(result["gap_pct"], self.cfg["trades"]["fairness"]["max_value_gap_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
