"""Storage backends must be interchangeable.

The whole point of the abstraction is that hosting changes where data lives and
nothing else. So every test here runs against *both* backends and asserts the
same result. If Postgres is unavailable, those runs skip rather than fail — the
file backend still gets full coverage.

To include Postgres locally:

    export FF_TEST_DATABASE_URL=postgresql://user@host/dbname
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from ff import config as C
from ff import storage
from ff.draft import Pick
from ff.storage.files import FileBackend

PG_URL = os.environ.get("FF_TEST_DATABASE_URL", "")


def postgres_backend():
    """A clean Postgres backend, or None if one is not available."""
    if not PG_URL:
        return None
    try:
        from ff.storage import postgres as pg

        backend = pg.PostgresBackend(PG_URL)
        # Wipe between tests so runs are independent.
        def clear(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "TRUNCATE leagues, picks, rosters, saved_trades,"
                    " projection_sets, platform_tokens, week_scores,"
                    " transactions, player_status RESTART IDENTITY CASCADE"
                )
        backend._run(clear)
        return backend
    except Exception:                                   # noqa: BLE001
        return None


def sample_league(name="Storage Test"):
    cfg = C.new_config(name, "manual", 4, 0.5)
    cfg["teams"] = [C.new_team(f"Team {i}") for i in range(1, 5)]
    cfg["my_team_id"] = cfg["teams"][0]["id"]
    return cfg


class BackendContract:
    """Shared assertions. Subclasses provide ``self.backend``."""

    backend: storage.Backend

    # -- leagues ----------------------------------------------------------

    def test_league_round_trip(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        loaded = self.backend.load_league(cfg["id"])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["name"], cfg["name"])
        self.assertEqual(len(loaded["teams"]), 4)
        # The whole config document survives, including nested rule blocks.
        self.assertEqual(loaded["scoring"], cfg["scoring"])
        self.assertEqual(loaded["trades"]["fairness"], cfg["trades"]["fairness"])

    def test_unknown_league_is_none_not_an_error(self):
        self.assertIsNone(self.backend.load_league("does-not-exist"))

    def test_save_is_an_upsert(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        cfg["name"] = "Renamed"
        self.backend.save_league(cfg)
        self.assertEqual(len(self.backend.list_leagues()), 1)
        self.assertEqual(self.backend.load_league(cfg["id"])["name"], "Renamed")

    def test_list_leagues_summarises(self):
        self.backend.save_league(sample_league("Alpha League"))
        self.backend.save_league(sample_league("Bravo League"))
        rows = {r["id"]: r for r in self.backend.list_leagues()}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["alpha-league"]["teams"], "4")

    def test_delete_league(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.delete_league(cfg["id"])
        self.assertIsNone(self.backend.load_league(cfg["id"]))

    # -- draft ------------------------------------------------------------

    def test_picks_round_trip(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        picks = [
            Pick(overall=1, round=1, pick_in_round=1, team_id="team-1",
                 player_id="bijan-robinson-RB", player_name="Bijan Robinson", pos="RB"),
            Pick(overall=2, round=1, pick_in_round=2, team_id="team-2",
                 player_id="jamarr-chase-WR", player_name="Ja'Marr Chase", pos="WR"),
        ]
        self.backend.save_picks(cfg["id"], [p.to_dict() for p in picks])
        rows = self.backend.load_picks(cfg["id"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["player_name"], "Bijan Robinson")
        self.assertEqual(rows[1]["overall"], 2)

    def test_save_picks_replaces_rather_than_appends(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        first = [{"overall": 1, "round": 1, "pick_in_round": 1, "team_id": "team-1",
                  "player_id": "a", "player_name": "A", "pos": "RB", "price": None}]
        self.backend.save_picks(cfg["id"], first)
        self.backend.save_picks(cfg["id"], first)
        self.assertEqual(len(self.backend.load_picks(cfg["id"])), 1)

    def test_clear_picks(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.save_picks(cfg["id"], [
            {"overall": 1, "round": 1, "pick_in_round": 1, "team_id": "team-1",
             "player_id": "a", "player_name": "A", "pos": "RB", "price": None}])
        self.backend.clear_picks(cfg["id"])
        self.assertEqual(self.backend.load_picks(cfg["id"]), [])

    # -- rosters ----------------------------------------------------------

    def test_roster_round_trip_and_replace(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.set_roster(cfg["id"], "team-1", ["a", "b", "c"])
        self.assertEqual(sorted(self.backend.load_rosters(cfg["id"])["team-1"]),
                         ["a", "b", "c"])
        self.backend.set_roster(cfg["id"], "team-1", ["d"])
        self.assertEqual(self.backend.load_rosters(cfg["id"])["team-1"], ["d"])

    def test_empty_roster_clears(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.set_roster(cfg["id"], "team-1", ["a"])
        self.backend.set_roster(cfg["id"], "team-1", [])
        self.assertEqual(self.backend.load_rosters(cfg["id"]).get("team-1", []), [])

    # -- trades -----------------------------------------------------------

    def test_trades_round_trip_newest_first(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.save_trade(cfg["id"], {"verdict": "win-win"}, "2026-08-10T00:00:00")
        self.backend.save_trade(cfg["id"], {"verdict": "favors-you"}, "2026-08-11T00:00:00")
        rows = self.backend.list_trades(cfg["id"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["verdict"], "favors-you")

    # -- weekly scores ----------------------------------------------------

    def test_week_scores_round_trip(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.set_week_scores(cfg["id"], 1, {"team-1": 118.4, "team-2": 96.0})
        self.backend.set_week_scores(cfg["id"], 2, {"team-1": 101.5})
        scores = self.backend.load_scores(cfg["id"])
        self.assertEqual(scores[1]["team-1"], 118.4)
        self.assertEqual(sorted(scores), [1, 2])

    def test_saving_a_week_replaces_it(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.set_week_scores(cfg["id"], 1, {"team-1": 100.0, "team-2": 90.0})
        self.backend.set_week_scores(cfg["id"], 1, {"team-1": 105.0})
        self.assertEqual(self.backend.load_scores(cfg["id"])[1], {"team-1": 105.0})

    def test_an_empty_week_clears_it(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.set_week_scores(cfg["id"], 1, {"team-1": 100.0})
        self.backend.set_week_scores(cfg["id"], 1, {})
        self.assertEqual(self.backend.load_scores(cfg["id"]), {})

    # -- transactions -----------------------------------------------------

    def test_transactions_round_trip_oldest_first(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        first = self.backend.add_transaction(cfg["id"], {
            "type": "add_drop", "team_id": "team-1", "adds": ["a"], "drops": ["b"],
            "week": 3, "date": "2026-09-22", "faab": 12.0, "note": "",
        })
        second = self.backend.add_transaction(cfg["id"], {
            "type": "trade", "team_id": "team-1", "partner_team_id": "team-2",
            "adds": ["c"], "drops": ["d"], "week": 4,
        })
        rows = self.backend.list_transactions(cfg["id"])
        self.assertEqual([r["id"] for r in rows], [first, second])
        # The player lists survive as lists, whatever the column type is.
        self.assertEqual(rows[0]["adds"], ["a"])
        self.assertEqual(rows[0]["drops"], ["b"])
        self.assertEqual(float(rows[0]["faab"]), 12.0)
        self.assertEqual(rows[1]["partner_team_id"], "team-2")

    def test_deleting_a_transaction_reports_whether_it_existed(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        txn_id = self.backend.add_transaction(cfg["id"], {
            "type": "add", "team_id": "team-1", "adds": ["a"]})
        self.assertTrue(self.backend.delete_transaction(cfg["id"], txn_id))
        self.assertFalse(self.backend.delete_transaction(cfg["id"], txn_id))
        self.assertEqual(self.backend.list_transactions(cfg["id"]), [])

    # -- weekly player status ---------------------------------------------

    def test_status_round_trip_is_scoped_to_one_week(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.set_status(cfg["id"], 3, "bijan-robinson-RB", "O")
        self.assertEqual(self.backend.load_statuses(cfg["id"], 3),
                         {"bijan-robinson-RB": "O"})
        # Next week starts clean; an injury note must not leak forward.
        self.assertEqual(self.backend.load_statuses(cfg["id"], 4), {})

    def test_an_empty_status_clears_the_override(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.set_status(cfg["id"], 3, "player-RB", "O")
        self.backend.set_status(cfg["id"], 3, "player-RB", "")
        self.assertEqual(self.backend.load_statuses(cfg["id"], 3), {})

    def test_setting_a_status_twice_updates_it(self):
        cfg = sample_league()
        self.backend.save_league(cfg)
        self.backend.set_status(cfg["id"], 3, "player-RB", "Q")
        self.backend.set_status(cfg["id"], 3, "player-RB", "O")
        self.assertEqual(self.backend.load_statuses(cfg["id"], 3), {"player-RB": "O"})

    # -- projections ------------------------------------------------------

    def test_projection_round_trip(self):
        csv = "Player,Pos,Rec Yds\nJa'Marr Chase,WR,1520\n"
        self.backend.save_projection_set("proj.csv", csv)
        self.assertEqual(
            [n for n, _ in self.backend.load_projection_texts()][0].replace(".csv", ""),
            "proj",
        )
        self.assertEqual(self.backend.load_projection_texts()[0][1], csv)
        self.assertEqual(len(self.backend.list_projection_sets()), 1)

    def test_projection_upload_replaces_same_name(self):
        self.backend.save_projection_set("proj.csv", "Player,Pos\nA,WR\n")
        self.backend.save_projection_set("proj.csv", "Player,Pos\nB,RB\n")
        texts = self.backend.load_projection_texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("B,RB", texts[0][1])

    def test_delete_projection_reports_whether_it_existed(self):
        self.backend.save_projection_set("proj.csv", "Player,Pos\nA,WR\n")
        self.assertTrue(self.backend.delete_projection_set("proj.csv"))
        self.assertFalse(self.backend.delete_projection_set("proj.csv"))

    # -- tokens -----------------------------------------------------------

    def test_token_round_trip(self):
        self.assertIsNone(self.backend.load_token("yahoo"))
        self.backend.save_token("yahoo", {"access_token": "abc", "expires_in": 3600})
        self.assertEqual(self.backend.load_token("yahoo")["access_token"], "abc")
        self.backend.save_token("yahoo", {"access_token": "def"})
        self.assertEqual(self.backend.load_token("yahoo")["access_token"], "def")


class TestFileBackend(BackendContract, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.backend = FileBackend(
            leagues_dir=root / "leagues",
            projections_dir=root / "projections",
            db_path=root / "state.db",
            secrets_dir=root / "secrets",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_league_is_readable_json_on_disk(self):
        """Configs stay hand-editable — that is why they are files."""
        cfg = sample_league()
        self.backend.save_league(cfg)
        path = self.backend.leagues_dir / f"{cfg['id']}.json"
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text())["name"], cfg["name"])


@unittest.skipIf(postgres_backend() is None,
                 "no Postgres available (set FF_TEST_DATABASE_URL)")
class TestPostgresBackend(BackendContract, unittest.TestCase):
    def setUp(self):
        backend = postgres_backend()
        if backend is None:
            self.skipTest("no Postgres available")
        self.backend = backend


class TestBackendSelection(unittest.TestCase):
    def tearDown(self):
        storage.reset()

    def test_no_database_url_means_local_files(self):
        env = dict(os.environ)
        env.pop("DATABASE_URL", None)
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            storage.reset()
            self.assertEqual(storage.get_backend().name, "files")

    def test_database_url_selects_postgres(self):
        with unittest.mock.patch.dict(
            os.environ, {"DATABASE_URL": "postgresql://u@h/db"}, clear=False
        ):
            storage.reset()
            # Constructing must not connect; that happens per operation.
            self.assertEqual(storage.get_backend().name, "postgres")

    def test_describe_never_leaks_the_password(self):
        from ff.storage.postgres import PostgresBackend

        backend = PostgresBackend("postgresql://user:sup3rsecret@db.example.com:5432/x")
        described = json.dumps(backend.describe())
        self.assertNotIn("sup3rsecret", described)
        self.assertIn("db.example.com", described)


if __name__ == "__main__":
    unittest.main()
