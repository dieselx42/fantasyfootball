"""The in-season HTTP routes.

The engines are tested directly elsewhere. What is tested here is the layer
that turns a URL into an engine call: which week a request means when it does
not say, that a bad move is refused with a readable reason rather than a 500,
and that a recorded transaction actually changes who is on the roster.

Routes are exercised through :func:`ff.web.server.dispatch`, which is the same
entry point the stdlib server and the WSGI adapter both use — so these tests
cover the hosted path as well as the local one.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs

from ff import config as C
from ff import storage, store
from ff.pool import build_pool, invalidate
from ff.storage.files import FileBackend
from ff.web.server import dispatch

PROJECTIONS = """Player,Team,Pos,Bye,Rush Yds,Rush TD,Rec,Rec Yds,Rec TD,Pass Yds,Pass TD,fg_40_49,dst_sack
Ace Passer,BUF,QB,7,300,3,0,0,0,4400,36,0,0
Backup Passer,CHI,QB,5,200,2,0,0,0,3600,24,0,0
Lead Back,ATL,RB,9,1500,14,50,400,2,0,0,0,0
Second Back,DAL,RB,10,1100,9,40,300,1,0,0,0,0
Third Back,CHI,RB,5,700,5,30,200,1,0,0,0,0
Spare Back,NYJ,RB,6,500,3,20,150,0,0,0,0,0
Top Wideout,CIN,WR,10,0,0,100,1400,11,0,0,0,0
Second Wideout,LAR,WR,6,0,0,88,1150,8,0,0,0,0
Third Wideout,SEA,WR,8,0,0,70,900,5,0,0,0,0
Spare Wideout,MIA,WR,12,0,0,55,700,3,0,0,0,0
Top End,LV,TE,8,0,0,90,1000,7,0,0,0,0
Spare End,DET,TE,5,0,0,55,600,3,0,0,0,0
Good Kicker,DAL,K,10,0,0,0,0,0,0,0,30,0
Spare Kicker,KC,K,10,0,0,0,0,0,0,0,20,0
Good Defense,BAL,DST,7,0,0,0,0,0,0,0,0,50
Spare Defense,PIT,DST,5,0,0,0,0,0,0,0,0,30
"""


def call(method, path, query="", body=None):
    return dispatch(method, path, parse_qs(query), body or {})


class InSeasonRoutes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        backend = FileBackend(
            leagues_dir=root / "leagues",
            projections_dir=root / "proj",
            db_path=root / "ff.db",
            secrets_dir=root / "secrets",
        )
        storage.set_backend(backend)
        invalidate()
        self.addCleanup(invalidate)
        self.addCleanup(storage.reset)

        cfg = C.new_config("Route Test", "manual", 4, 1.0)
        cfg["teams"] = [C.new_team(f"Team {i}") for i in range(1, 5)]
        cfg["my_team_id"] = cfg["teams"][0]["id"]
        C.save(cfg)
        self.cfg = cfg
        self.lid = cfg["id"]
        self.me = cfg["my_team_id"]

        backend.save_projection_set("season.csv", PROJECTIONS)
        invalidate()
        pool = build_pool(cfg)
        self.pool = {p.name: p.player_id for p in pool}
        for i, team in enumerate(cfg["teams"]):
            store.set_roster(self.lid, team["id"], [p.player_id for p in pool[i::4]])

    # -- which week ------------------------------------------------------

    def test_week_defaults_to_the_first_unscored_one(self):
        status, data = call("GET", f"/api/league/{self.lid}/week")
        self.assertEqual(status, 200)
        self.assertEqual(data["week"], 1)

        store.set_week_scores(self.lid, 1, {t["id"]: 100.0 for t in self.cfg["teams"]})
        _, data = call("GET", f"/api/league/{self.lid}/week")
        self.assertEqual(data["week"], 2)

    def test_an_explicit_week_wins(self):
        _, data = call("GET", f"/api/league/{self.lid}/week", "week=9")
        self.assertEqual(data["week"], 9)

    def test_a_nonsense_week_is_a_readable_400(self):
        status, data = call("GET", f"/api/league/{self.lid}/week", "week=soon")
        self.assertEqual(status, 400)
        self.assertIn("not a week number", data["error"])

    # -- the weekly view -------------------------------------------------

    def test_the_week_view_fills_a_lineup_and_a_matchup(self):
        _, data = call("GET", f"/api/league/{self.lid}/week", "week=3")
        self.assertTrue(any(row["player"] for row in data["lineup"]))
        self.assertGreater(data["projected"], 0)
        self.assertIsNotNone(data["matchup"])
        self.assertEqual(data["matchup"]["result"], "pending")
        # No week-specific file was imported, and the response says so.
        self.assertFalse(data["weekly_projections"])

    def test_marking_a_player_out_lowers_the_projection(self):
        _, before = call("GET", f"/api/league/{self.lid}/week", "week=3")
        starter = next(r["player"] for r in before["lineup"] if r["player"])

        status, _ = call("POST", f"/api/league/{self.lid}/week/status", "week=3",
                         {"player_id": starter["player_id"], "status": "Out"})
        self.assertEqual(status, 200)

        _, after = call("GET", f"/api/league/{self.lid}/week", "week=3")
        self.assertLess(after["projected"], before["projected"])
        self.assertIn(starter["name"], [p["name"] for p in after["unavailable"]])
        # ...and only for that week.
        _, other = call("GET", f"/api/league/{self.lid}/week", "week=4")
        self.assertEqual(other["unavailable"], [])

    def test_a_status_needs_a_player(self):
        status, data = call("POST", f"/api/league/{self.lid}/week/status", "week=3", {})
        self.assertEqual(status, 400)
        self.assertIn("Which player", data["error"])

    # -- waivers ---------------------------------------------------------

    def test_waivers_only_offer_players_nobody_owns(self):
        _, data = call("GET", f"/api/league/{self.lid}/waivers", "week=3")
        owned = {pid for ids in store.current_roster_ids(self.lid).values() for pid in ids}
        self.assertTrue(owned)
        for row in data["recommendations"]:
            self.assertNotIn(row["player"]["player_id"], owned)

    def test_waivers_pair_an_add_with_a_drop_once_the_roster_is_full(self):
        # Four players each, so a four-slot roster with no bench is full.
        self.cfg["roster"] = {
            "slots": [
                {"slot": "QB", "count": 1, "eligible": ["QB"]},
                {"slot": "RB", "count": 1, "eligible": ["RB"]},
                {"slot": "WR", "count": 1, "eligible": ["WR"]},
                {"slot": "FLEX", "count": 1, "eligible": ["RB", "WR", "TE"]},
            ],
            "bench": 0,
            "ir": 0,
        }
        C.save(self.cfg)
        invalidate()
        _, data = call("GET", f"/api/league/{self.lid}/waivers", "week=3")
        self.assertTrue(data["roster_full"])
        self.assertTrue(all(r["drop"] for r in data["recommendations"]))

    def test_waivers_report_the_faab_state_only_for_faab_leagues(self):
        _, data = call("GET", f"/api/league/{self.lid}/waivers", "week=3")
        self.assertIsNone(data["faab"])

        self.cfg["waivers"]["type"] = "faab"
        self.cfg["waivers"]["faab_budget"] = 100
        C.save(self.cfg)
        _, data = call("GET", f"/api/league/{self.lid}/waivers", "week=3")
        self.assertEqual(data["faab"]["budget"], 100.0)

    def test_faab_already_spent_comes_off_the_budget(self):
        self.cfg["waivers"]["type"] = "faab"
        self.cfg["waivers"]["faab_budget"] = 100
        C.save(self.cfg)
        call("POST", f"/api/league/{self.lid}/transactions", "week=1",
             {"type": "add", "team_id": self.me,
              "adds": [self.pool["Spare Kicker"]], "faab": 30})
        _, data = call("GET", f"/api/league/{self.lid}/waivers", "week=3")
        self.assertEqual(data["faab"]["remaining"], 70.0)

    # -- scores and standings --------------------------------------------

    def test_recording_scores_drives_the_standings(self):
        ids = [t["id"] for t in self.cfg["teams"]]
        status, _ = call("POST", f"/api/league/{self.lid}/scores", "week=1",
                         {"scores": {ids[0]: 120, ids[1]: 100, ids[2]: 90, ids[3]: 95}})
        self.assertEqual(status, 200)

        _, data = call("GET", f"/api/league/{self.lid}/standings")
        rows = {r["team_id"]: r for r in data["standings"]}
        self.assertEqual(rows[ids[0]]["points_for"], 120.0)
        self.assertEqual(sum(r["games_played"] for r in rows.values()), 4)

    def test_an_unknown_team_in_a_score_sheet_is_refused(self):
        status, data = call("POST", f"/api/league/{self.lid}/scores", "week=1",
                            {"scores": {"ghost": 100}})
        self.assertEqual(status, 400)
        self.assertIn("ghost", data["error"])

    def test_a_score_that_is_not_a_number_is_refused(self):
        ids = [t["id"] for t in self.cfg["teams"]]
        status, data = call("POST", f"/api/league/{self.lid}/scores", "week=1",
                            {"scores": {ids[0]: "lots"}})
        self.assertEqual(status, 400)
        self.assertIn("not a score", data["error"])

    def test_blank_scores_are_skipped_not_zeroed(self):
        ids = [t["id"] for t in self.cfg["teams"]]
        call("POST", f"/api/league/{self.lid}/scores", "week=1",
             {"scores": {ids[0]: 120, ids[1]: ""}})
        self.assertEqual(store.load_scores(self.lid)[1], {ids[0]: 120.0})

    def test_the_scoreboard_projects_before_it_is_final(self):
        _, data = call("GET", f"/api/league/{self.lid}/scoreboard", "week=2")
        self.assertFalse(data["complete"])
        self.assertEqual(len(data["games"]), 2)
        self.assertIsNotNone(data["games"][0]["home_projected"])
        self.assertIsNone(data["games"][0]["home_points"])

    # -- transactions ----------------------------------------------------

    def test_a_recorded_move_changes_the_roster(self):
        add = self.pool["Spare Kicker"]
        drop = store.current_roster_ids(self.lid)[self.me][0]
        status, data = call("POST", f"/api/league/{self.lid}/transactions", "week=2",
                            {"type": "add_drop", "team_id": self.me,
                             "adds": [add], "drops": [drop]})
        self.assertEqual(status, 200)

        roster = store.current_roster_ids(self.lid)[self.me]
        self.assertIn(add, roster)
        self.assertNotIn(drop, roster)

        # ...and deleting it puts things back.
        call("DELETE", f"/api/league/{self.lid}/transactions/{data['transaction']['id']}")
        roster = store.current_roster_ids(self.lid)[self.me]
        self.assertNotIn(add, roster)
        self.assertIn(drop, roster)

    def test_a_trade_moves_players_both_ways(self):
        them = self.cfg["teams"][1]["id"]
        mine = store.current_roster_ids(self.lid)[self.me][0]
        theirs = store.current_roster_ids(self.lid)[them][0]
        call("POST", f"/api/league/{self.lid}/transactions", "week=2",
             {"type": "trade", "team_id": self.me, "partner_team_id": them,
              "adds": [theirs], "drops": [mine]})
        rosters = store.current_roster_ids(self.lid)
        self.assertIn(theirs, rosters[self.me])
        self.assertIn(mine, rosters[them])

    def test_an_invalid_move_is_refused_with_a_reason(self):
        status, data = call("POST", f"/api/league/{self.lid}/transactions", "week=2",
                            {"type": "trade", "team_id": self.me, "adds": ["x"]})
        self.assertEqual(status, 400)
        self.assertIn("second team", data["error"])

    def test_a_move_gets_a_week_and_a_timestamp_by_default(self):
        _, data = call("POST", f"/api/league/{self.lid}/transactions",
                       body={"type": "add", "team_id": self.me,
                             "adds": [self.pool["Spare Kicker"]]})
        self.assertEqual(data["transaction"]["week"], 1)
        self.assertTrue(data["transaction"]["date"])

    def test_deleting_a_move_that_is_not_there(self):
        status, _ = call("DELETE", f"/api/league/{self.lid}/transactions/999")
        self.assertEqual(status, 404)
        status, _ = call("DELETE", f"/api/league/{self.lid}/transactions/abc")
        self.assertEqual(status, 400)

    def test_the_activity_feed_reads_back(self):
        call("POST", f"/api/league/{self.lid}/transactions", "week=2",
             {"type": "add", "team_id": self.me,
              "adds": [self.pool["Spare Kicker"]], "faab": 5})
        _, data = call("GET", f"/api/league/{self.lid}/transactions")
        self.assertEqual(data["total"], 1)
        self.assertIn("Spare Kicker", data["feed"][0]["summary"])
        self.assertEqual(data["positions_chased"], [{"pos": "K", "adds": 1}])

    # -- schedule --------------------------------------------------------

    def test_the_schedule_is_generated_and_says_so(self):
        _, data = call("GET", f"/api/league/{self.lid}/schedule")
        self.assertTrue(data["generated"])
        self.assertEqual(len(data["schedule"][0]["games"]), 2)

    def test_an_unknown_league_is_a_404(self):
        status, _ = call("GET", "/api/league/nope/week")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
