"""Platform adapters and the sync layer.

The parsing here is the code most likely to be wrong and least likely to
announce it. A misread scoring rule changes every valuation in the app with
nothing on screen to explain why; a team id that fails to join drops a fixture
out of the standings. Both are silent, so both are tested.

Yahoo's API is not reachable from a test run — it needs OAuth and a live
network — so its parsers run against the shape fixtures in
``tests/fixtures/yahoo_payloads``. Sleeper's public API *is* reachable, and the
live test against it is opt-in via ``FF_TEST_LIVE_SLEEPER=1``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ff import config as C
from ff import storage, store, sync
from ff.platforms import CAPABILITIES, PlatformUnsupported, get_adapter
from ff.platforms import sleeper as S
from ff.platforms import yahoo as Y
from ff.players import Player, make_player_id
from ff.pool import invalidate
from ff.storage.files import FileBackend
from tests.fixtures import yahoo_payloads as F

LIVE = os.environ.get("FF_TEST_LIVE_SLEEPER") == "1"


def fake_yahoo(**overrides):
    """A Yahoo adapter wired to the fixtures instead of the network."""
    routes = {
        "stat_categories": F.STAT_CATEGORIES,
        "settings": F.SETTINGS,
        "scoreboard": F.SCOREBOARD,
        "transactions": F.TRANSACTIONS,
        "draftresults": F.DRAFT_RESULTS,
        "players;player_keys": F.PLAYERS_BY_KEY,
        "teams/roster": F.ROSTERS,
        "teams": F.TEAMS,
    }
    routes.update(overrides)

    class Fake(Y.YahooAdapter):
        def _get(self, path):
            for fragment, payload in routes.items():
                if fragment in path:
                    return payload
            raise AssertionError(f"unexpected path {path}")

    return Fake({"league_id": "184206"})


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------

class TestContract(unittest.TestCase):
    def test_every_adapter_declares_only_known_capabilities(self):
        for kind in ("yahoo", "sleeper", "espn", "manual"):
            for capability in get_adapter(kind).capabilities():
                self.assertIn(capability, CAPABILITIES, f"{kind}: {capability}")

    def test_an_unsupported_operation_says_which_and_why(self):
        adapter = get_adapter("manual")
        with self.assertRaises(PlatformUnsupported) as caught:
            adapter.fetch_scoreboard("1", 3)
        self.assertIn("Manual", str(caught.exception))
        self.assertIn("weekly scores", str(caught.exception))

    def test_capabilities_are_described_for_the_ui(self):
        described = get_adapter("espn").describe_capabilities()
        self.assertEqual(len(described), len(CAPABILITIES))
        supported = {row["key"] for row in described if row["supported"]}
        self.assertEqual(supported, {"league", "rosters", "players"})
        self.assertTrue(all(row["label"] for row in described))


# --------------------------------------------------------------------------
# Yahoo
# --------------------------------------------------------------------------

class TestAuthorizationCode(unittest.TestCase):
    """Whatever gets pasted out of the address bar has to work.

    Yahoo rejects a malformed code with an opaque invalid_grant, at the last
    step of a setup nobody does twice, so guessing wrong is expensive.
    """

    def test_every_way_a_code_gets_pasted(self):
        for raw in (
            "abc123d",
            "code=abc123d",
            "?code=abc123d",
            "https://localhost:8000/?code=abc123d",
            "https://localhost:8000/?code=abc123d&state=xyz",
            "  code=abc123d  ",
        ):
            self.assertEqual(Y.extract_code(raw), "abc123d", raw)

    def test_nothing_pasted_stays_nothing(self):
        self.assertEqual(Y.extract_code(""), "")
        self.assertEqual(Y.extract_code("   "), "")

    def test_the_redirect_uri_is_configurable_and_matches_the_authorize_url(self):
        """The registered URI and the one we send must agree exactly, so the
        value used to build the link is the value used to redeem the code."""
        import urllib.parse as parse

        adapter = get_adapter("yahoo", {"client_id": "ABC", "redirect_uri": "oob"})
        query = dict(parse.parse_qsl(parse.urlparse(adapter.authorize_url()).query))
        self.assertEqual(query["redirect_uri"], "oob")
        self.assertEqual(adapter.redirect_uri, "oob")

    def test_the_authorize_url_asks_for_fantasy_access(self):
        """Without a scope, Yahoo mints a token that authorises cleanly and
        then 401s on every fantasy endpoint — the failure arrives one step
        after the mistake, which is why it is worth pinning down here."""
        import urllib.parse as parse

        adapter = get_adapter("yahoo", {"client_id": "ABC"})
        query = dict(parse.parse_qsl(parse.urlparse(adapter.authorize_url()).query))
        self.assertEqual(query["scope"], "fspt-r")

    def test_scope_is_configurable_for_write_access(self):
        adapter = get_adapter("yahoo", {"client_id": "ABC", "scope": "fspt-w"})
        self.assertIn("scope=fspt-w", adapter.authorize_url())

    def test_the_default_redirect_uri_is_one_yahoo_will_accept(self):
        adapter = get_adapter("yahoo", {"client_id": "ABC"})
        self.assertEqual(adapter.redirect_uri, Y.DEFAULT_REDIRECT_URI)
        self.assertTrue(adapter.redirect_uri.startswith("https://"))


class TestYahooShape(unittest.TestCase):
    """Yahoo splits one object's fields across a list of one-key dicts."""

    def test_merge_reassembles_a_split_object(self):
        merged = Y._merge([[{"player_key": "nfl.p.1"}, {"name": {"full": "A"}}],
                           {"display_position": "RB"}])
        self.assertEqual(merged["player_key"], "nfl.p.1")
        self.assertEqual(merged["display_position"], "RB")

    def test_merge_keeps_count_because_a_roster_slot_uses_it(self):
        """Yahoo uses 'count' both as a collection length and as the number of
        slots at a position. Dropping it loses the roster shape."""
        self.assertEqual(Y._merge({"position": "RB", "count": 2})["count"], 2)

    def test_elements_finds_objects_at_any_depth(self):
        self.assertEqual(len(Y._elements(F.SCOREBOARD, "matchup")), 2)
        self.assertEqual(len(Y._elements(F.PLAYERS_BY_KEY, "player")), 3)


class TestYahooRules(unittest.TestCase):
    def setUp(self):
        self.rules = fake_yahoo().fetch_rules("184206")

    def test_scoring_matches_the_real_league(self):
        """The fixture is the Keg South ruleset, whose correct config is
        checked into leagues/. Any mapping error shows up as a mismatch."""
        real = C.load_file(Path("leagues/keg-south.json"))["scoring"]
        got = self.rules["scoring"]
        shared = set(got) & set(real)
        self.assertGreater(len(shared), 15)
        for key in sorted(shared):
            self.assertAlmostEqual(float(got[key]), float(real[key]), places=6, msg=key)

    def test_the_same_word_maps_differently_by_side_of_the_ball(self):
        self.assertEqual(Y.map_stat_name("Interceptions", "O"), "pass_int")
        self.assertEqual(Y.map_stat_name("Interceptions", "DT"), "dst_int")

    def test_points_allowed_becomes_one_banded_rule(self):
        bands = self.rules["scoring_bands"]["dst_pa"]
        real = C.load_file(Path("leagues/keg-south.json"))["scoring_bands"]["dst_pa"]
        self.assertEqual([b["max"] for b in bands], [b["max"] for b in real])
        self.assertEqual([b["points"] for b in bands], [b["points"] for b in real])

    def test_the_band_list_ends_open_ended(self):
        """config.validate rejects anything else, since otherwise some scores
        would silently count for nothing."""
        self.assertIsNone(self.rules["scoring_bands"]["dst_pa"][-1]["max"])

    def test_an_untranslatable_rule_is_reported_not_dropped(self):
        names = [u["name"] for u in self.rules["unmapped"]]
        self.assertIn("Targets Inside The 10", names)
        self.assertTrue(all(u["why"] for u in self.rules["unmapped"]))

    def test_roster_slots_keep_yahoo_flex_shape(self):
        slots = {s["slot"]: s for s in self.rules["roster"]["slots"]}
        self.assertEqual(slots["W/R/T"]["eligible"], ["RB", "WR", "TE"])
        self.assertEqual(slots["RB"]["count"], 2)
        self.assertEqual(self.rules["roster"]["bench"], 6)

    def test_the_defence_slot_is_renamed_to_our_vocabulary(self):
        slots = {s["slot"] for s in self.rules["roster"]["slots"]}
        self.assertIn("DST", slots)
        self.assertNotIn("DEF", slots)

    def test_playoffs_and_team_count(self):
        self.assertEqual(self.rules["playoffs"], {"teams": 6, "weeks": [15, 16, 17]})
        self.assertEqual(self.rules["team_count"], 10)

    def test_the_imported_rules_produce_a_valid_config(self):
        cfg = C.new_config("Imported", "yahoo", 10, 1.0)
        cfg["teams"] = [C.new_team(f"Team {i}") for i in range(1, 11)]
        sync.apply_rules(cfg, self.rules)
        self.assertEqual(C.validate(cfg), [])


class TestYahooInSeason(unittest.TestCase):
    def test_scoreboard_pairs_teams_and_reads_points(self):
        board = fake_yahoo().fetch_scoreboard("184206", 3)
        self.assertEqual(board["games"], [{"home": "1", "away": "7"},
                                          {"home": "2", "away": "5"}])
        self.assertAlmostEqual(board["scores"]["1"], 118.42)
        self.assertTrue(board["final"])

    def test_a_week_not_yet_played_has_fixtures_but_no_scores(self):
        board = Y._parse_scoreboard(F.SCOREBOARD_PENDING, 4)
        self.assertEqual(len(board["games"]), 1)
        self.assertEqual(board["scores"], {})
        self.assertFalse(board["final"])

    def test_transactions_carry_position_so_ids_reconstruct(self):
        rows = fake_yahoo().fetch_transactions("184206")
        add = rows[0]["adds"][0]
        self.assertEqual(add["name"], "C.J. Stroud")
        self.assertEqual(add["pos"], "QB")

    def test_an_add_drop_is_one_move_with_both_sides(self):
        rows = fake_yahoo().fetch_transactions("184206")
        self.assertEqual(rows[0]["type"], "add_drop")
        self.assertEqual(rows[0]["team_id"], "1")
        self.assertEqual(rows[0]["faab"], 14.0)
        self.assertEqual([d["name"] for d in rows[0]["drops"]], ["Harrison Butker"])

    def test_a_trade_names_both_teams(self):
        trade = fake_yahoo().fetch_transactions("184206")[1]
        self.assertEqual(trade["type"], "trade")
        self.assertEqual({trade["team_id"], trade["partner_team_id"]}, {"1", "4"})

    def test_a_vetoed_move_never_happened(self):
        rows = fake_yahoo().fetch_transactions("184206")
        self.assertEqual(len(rows), 2)
        self.assertNotIn("Ghost Player",
                         [a["name"] for r in rows for a in r["adds"]])

    def test_draft_results_resolve_player_names(self):
        picks = fake_yahoo().fetch_draft_results("184206")
        self.assertEqual([p["name"] for p in picks],
                         ["Ja'Marr Chase", "Bijan Robinson", "Baltimore"])
        self.assertEqual(picks[2]["pos"], "DST")

    def test_rosters_come_back_in_our_player_ids(self):
        rosters = fake_yahoo().fetch_rosters("184206")
        self.assertEqual(rosters, {"1": ["bijan-robinson-RB"]})

    def test_teams_keep_their_platform_key(self):
        league = fake_yahoo().fetch_league("184206")
        keys = {t["id"]: t["platform_key"] for t in league["teams"]}
        self.assertEqual(keys["1"], "nfl.l.184206.t.1")
        self.assertEqual(league["teams"][0]["manager"], "Phil")


# --------------------------------------------------------------------------
# Sleeper
# --------------------------------------------------------------------------

class TestSleeper(unittest.TestCase):
    def test_points_allowed_keys_become_bands(self):
        self.assertEqual(S._points_allowed_band("pts_allow_0"), 0)
        self.assertEqual(S._points_allowed_band("pts_allow_1_6"), 6)
        self.assertIsNone(S._points_allowed_band("pts_allow_35p"))
        self.assertIs(S._points_allowed_band("rec_yd"), False)

    def test_a_waiver_claim_becomes_an_add_drop(self):
        index = {"1": {"player_id": "a-RB", "name": "A Back", "pos": "RB", "team": "BAL"},
                 "2": {"player_id": "b-WR", "name": "B Wide", "pos": "WR", "team": "LAR"}}
        row = {"type": "waiver", "status": "complete", "adds": {"1": 3},
               "drops": {"2": 3}, "roster_ids": [3],
               "settings": {"waiver_bid": 12}, "status_updated": 1}
        parsed = S._parse_transaction(row, index, {"3": "3"}, 2)
        self.assertEqual(parsed["type"], "add_drop")
        self.assertEqual(parsed["team_id"], "3")
        self.assertEqual(parsed["faab"], 12.0)
        self.assertEqual(parsed["adds"][0]["name"], "A Back")

    def test_a_trade_is_read_from_which_roster_ends_up_with_whom(self):
        index = {"1": {"player_id": "a-RB", "name": "A Back", "pos": "RB", "team": ""},
                 "2": {"player_id": "b-WR", "name": "B Wide", "pos": "WR", "team": ""}}
        row = {"type": "trade", "status": "complete",
               "adds": {"1": 3, "2": 4}, "drops": {"1": 4, "2": 3},
               "roster_ids": [3, 4], "status_updated": 1}
        parsed = S._parse_transaction(row, index, {"3": "3", "4": "4"}, 5)
        self.assertEqual(parsed["type"], "trade")
        self.assertEqual(parsed["team_id"], "3")
        self.assertEqual(parsed["partner_team_id"], "4")
        self.assertEqual([a["name"] for a in parsed["adds"]], ["A Back"])
        self.assertEqual([d["name"] for d in parsed["drops"]], ["B Wide"])

    def test_an_incomplete_move_is_skipped(self):
        row = {"type": "waiver", "status": "failed", "adds": {"1": 3},
               "roster_ids": [3]}
        self.assertIsNone(S._parse_transaction(row, {}, {"3": "3"}, 1))

    @unittest.skipUnless(LIVE, "set FF_TEST_LIVE_SLEEPER=1 to hit the real API")
    def test_live_player_index_translates_sleeper_ids_to_ours(self):
        index = SleeperLive()._player_index()
        self.assertGreater(len(index), 2000)
        positions = {row["pos"] for row in index.values()}
        self.assertTrue({"QB", "RB", "WR", "TE", "K", "DST"} <= positions)
        for row in index.values():
            self.assertEqual(row["player_id"],
                             make_player_id(row["name"], row["pos"], row["team"]))


def SleeperLive():
    return S.SleeperAdapter({"league_id": "0"})


# --------------------------------------------------------------------------
# The sync layer
# --------------------------------------------------------------------------

def player(name, pos):
    return Player(player_id=make_player_id(name, pos, ""), name=name, pos=pos)


class TestCredentialsStayOutOfConfigs(unittest.TestCase):
    """League configs are meant to be committed and shared with league-mates.
    An API secret in one is a secret in someone's git history."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        storage.set_backend(FileBackend(
            leagues_dir=self.root / "leagues", projections_dir=self.root / "proj",
            db_path=self.root / "ff.db", secrets_dir=self.root / "secrets"))
        self.addCleanup(storage.reset)

        cfg = C.new_config("Secrets", "yahoo", 2, 1.0)
        cfg["teams"] = [C.new_team("A"), C.new_team("B")]
        cfg["platform"]["settings"] = {
            "league_id": "184206",
            "client_id": "PUBLIC-ID",
            "client_secret": "SUPER-SECRET",
        }
        C.save(cfg)
        self.cfg = cfg

    def league_file(self):
        return (self.root / "leagues" / "secrets.json").read_text()

    def test_the_secret_never_reaches_the_league_file(self):
        self.assertNotIn("SUPER-SECRET", self.league_file())
        self.assertNotIn("PUBLIC-ID", self.league_file())

    def test_real_configuration_still_does(self):
        self.assertIn("184206", self.league_file())

    def test_the_adapter_still_finds_them(self):
        adapter = get_adapter("yahoo", {})
        self.assertEqual(adapter.credential("client_secret"), "SUPER-SECRET")
        self.assertIn("PUBLIC-ID", adapter.authorize_url())

    def test_a_later_save_does_not_wipe_them(self):
        """Saving a league whose config no longer carries credentials — which
        is every save after the first — must not clear the stored ones."""
        loaded = C.load("secrets")
        loaded["name"] = "Renamed"
        C.save(loaded)
        self.assertEqual(get_adapter("yahoo", {}).credential("client_secret"),
                         "SUPER-SECRET")

    def test_values_passed_in_directly_win(self):
        """The setup wizard authorises with typed-in credentials before the
        league has been saved at all."""
        adapter = get_adapter("yahoo", {"client_id": "TYPED"})
        self.assertEqual(adapter.credential("client_id"), "TYPED")


class TestJoins(unittest.TestCase):
    def setUp(self):
        self.cfg = C.new_config("Join Test", "yahoo", 2, 1.0)
        self.cfg["teams"] = [
            {"id": "diesel", "name": "DIESEL", "manager": "",
             "platform_key": "nfl.l.1.t.1"},
            {"id": "killers", "name": "The Killer Ps", "manager": ""},
        ]

    def test_a_platform_key_joins_by_full_key_or_bare_id(self):
        mapping = sync.team_map(self.cfg)
        self.assertEqual(sync.resolve_team(mapping, "nfl.l.1.t.1"), "diesel")
        self.assertEqual(sync.resolve_team(mapping, "1"), "diesel")

    def test_a_team_without_a_key_still_joins_by_name(self):
        mapping = sync.team_map(self.cfg)
        self.assertEqual(sync.resolve_team(mapping, "the-killer-ps"), "killers")

    def test_an_unknown_team_resolves_to_nothing_rather_than_a_guess(self):
        self.assertIsNone(sync.resolve_team(sync.team_map(self.cfg), "t.99"))

    def test_the_hint_explains_a_league_with_no_platform_keys(self):
        cfg = C.new_config("Bare", "yahoo", 2, 1.0)
        cfg["teams"] = [C.new_team("A"), C.new_team("B")]
        self.assertIn("Sync 'Teams and managers' first", sync._team_hint(cfg, ["1"]))

    def test_players_resolve_by_name_and_position(self):
        pool = [player("Bijan Robinson", "RB"), player("Josh Allen", "QB")]
        index = sync.player_index(pool)
        self.assertEqual(
            sync.resolve_player(index, {"name": "Bijan Robinson", "pos": "RB"}),
            "bijan-robinson-RB")
        self.assertEqual(sync.resolve_player(index, "Josh Allen"), "josh-allen-QB")

    def test_position_disambiguates_a_shared_name(self):
        pool = [player("Mike Williams", "WR"), player("Mike Williams", "TE")]
        index = sync.player_index(pool)
        self.assertEqual(
            sync.resolve_player(index, {"name": "Mike Williams", "pos": "TE"}),
            "mike-williams-TE")

    def test_a_player_we_do_not_have_resolves_to_nothing(self):
        index = sync.player_index([player("Bijan Robinson", "RB")])
        self.assertIsNone(sync.resolve_player(index, {"name": "Nobody", "pos": "RB"}))


class TestSyncOperations(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        storage.set_backend(FileBackend(
            leagues_dir=root / "leagues", projections_dir=root / "proj",
            db_path=root / "ff.db", secrets_dir=root / "secrets"))
        invalidate()
        self.addCleanup(invalidate)
        self.addCleanup(storage.reset)

        cfg = C.new_config("Keg", "yahoo", 2, 1.0)
        cfg["platform"]["settings"]["league_id"] = "184206"
        cfg["teams"] = [
            {"id": "diesel", "name": "DIESEL", "manager": "",
             "platform_key": "nfl.l.184206.t.1"},
            {"id": "killers", "name": "The Killer Ps", "manager": "",
             "platform_key": "nfl.l.184206.t.7"},
        ]
        cfg["my_team_id"] = "diesel"
        C.save(cfg)
        self.cfg = cfg
        self.pool = [player(n, p) for n, p in [
            ("C.J. Stroud", "QB"), ("Harrison Butker", "K"),
            ("Puka Nacua", "WR"), ("Bijan Robinson", "RB")]]

        self._real = sync.adapter_for
        sync.adapter_for = lambda _cfg: fake_yahoo()
        self.addCleanup(lambda: setattr(sync, "adapter_for", self._real))

    def test_scoreboard_writes_scores_and_the_real_fixtures(self):
        result = sync.sync_scoreboard(self.cfg, 3)
        self.assertEqual(result["teams_scored"], 2)
        self.assertEqual(result["games"], [{"home": "diesel", "away": "killers"}])
        stored = store.load_scores(self.cfg["id"])[3]
        self.assertAlmostEqual(stored["diesel"], 118.42)

    def test_unmatched_teams_are_named_not_swallowed(self):
        result = sync.sync_scoreboard(self.cfg, 3)
        # The fixture has four teams; this league only knows two.
        self.assertEqual(result["unmatched_teams"], ["2", "5"])

    def test_the_platform_schedule_replaces_a_generated_one(self):
        result = sync.sync_scoreboard(self.cfg, 3)
        self.assertTrue(sync.merge_schedule(self.cfg, 3, result["games"]))
        weeks = [e["week"] for e in self.cfg["schedule"]]
        self.assertEqual(weeks, [3])
        # Syncing the same week again replaces rather than duplicates it.
        sync.merge_schedule(self.cfg, 3, result["games"])
        self.assertEqual(len(self.cfg["schedule"]), 1)

    def test_transactions_import_and_do_not_duplicate(self):
        first = sync.sync_transactions(self.cfg, self.pool)
        self.assertEqual(first["imported"], 2)
        again = sync.sync_transactions(self.cfg, self.pool)
        self.assertEqual(again["imported"], 0)
        self.assertEqual(again["already_recorded"], 2)
        self.assertEqual(len(store.list_transactions(self.cfg["id"])), 2)

    def test_an_imported_move_changes_the_roster(self):
        store.set_roster(self.cfg["id"], "diesel", ["harrison-butker-K"])
        sync.sync_transactions(self.cfg, self.pool)
        roster = store.current_roster_ids(self.cfg["id"])["diesel"]
        self.assertIn("cj-stroud-QB", roster)
        self.assertNotIn("harrison-butker-K", roster)

    def test_players_missing_from_the_pool_are_reported(self):
        result = sync.sync_transactions(self.cfg, [player("C.J. Stroud", "QB")])
        self.assertIn("Harrison Butker", result["players_not_in_pool"])

    def test_syncing_teams_records_platform_keys(self):
        cfg = C.new_config("Fresh", "yahoo", 2, 1.0)
        cfg["platform"]["settings"]["league_id"] = "184206"
        result = sync.sync_league(cfg)
        self.assertEqual(result["teams"], 2)
        self.assertTrue(all(t["platform_key"] for t in cfg["teams"]))

    def test_losing_teams_in_a_sync_is_called_out(self):
        cfg = dict(self.cfg)
        cfg["teams"] = self.cfg["teams"] + [C.new_team(f"Extra {i}") for i in range(3)]
        result = sync.sync_league(cfg)
        self.assertEqual(result["teams"], 2)
        self.assertTrue(result["dropped_teams"])
        self.assertIn("orphaned", result["hint"])

    def test_matching_team_names_keep_the_draft_order(self):
        """The common case: nobody renamed anything, so a team import must
        leave the draft order exactly as it was."""
        cfg = dict(self.cfg)
        cfg["teams"] = [
            {"id": "diesel", "name": "DIESEL", "manager": ""},
            {"id": "the-killer-ps", "name": "The Killer Ps", "manager": ""},
        ]
        cfg["draft"] = {**cfg["draft"], "order": ["the-killer-ps", "diesel"]}
        sync.sync_league(cfg)
        self.assertEqual(cfg["draft"]["order"], ["the-killer-ps", "diesel"])
        self.assertEqual(C.validate(cfg), [])

    def test_a_stale_draft_order_is_cleared_rather_than_left_dangling(self):
        """Team ids come from team names, so a rename on the platform orphans
        the draft order — which fails validation and rejects the whole import
        with an error about the draft order, of all things."""
        cfg = dict(self.cfg)
        cfg["teams"] = list(self.cfg["teams"]) + [C.new_team("Gone Team")]
        cfg["team_count"] = 3
        cfg["draft"] = {**cfg["draft"],
                        "order": ["diesel", "killers", "gone-team"]}
        result = sync.sync_league(cfg)
        self.assertTrue(result["draft_order_cleared"])
        self.assertEqual(cfg["draft"]["order"], [])
        self.assertEqual(C.validate(cfg), [])

    def test_my_team_survives_or_is_reset_to_something_real(self):
        cfg = dict(self.cfg)
        cfg["my_team_id"] = "a-team-that-is-gone"
        sync.sync_league(cfg)
        self.assertIn(cfg["my_team_id"], [t["id"] for t in cfg["teams"]])

    def test_rules_are_proposed_not_applied(self):
        before = dict(self.cfg["scoring"])
        result = sync.sync_rules(self.cfg)
        self.assertFalse(result["applied"])
        self.assertEqual(self.cfg["scoring"], before)
        self.assertTrue(result["changes"])

    def test_applying_rules_never_writes_team_count(self):
        """The teams sync owns team_count; writing it here can leave a config
        claiming ten teams while listing two, which fails validation."""
        proposed = sync.sync_rules(self.cfg)["proposed"]
        self.assertIn("team_count", proposed)
        sync.apply_rules(self.cfg, proposed)
        self.assertEqual(self.cfg["team_count"], 2)
        self.assertEqual(C.validate(self.cfg), [])


if __name__ == "__main__":
    unittest.main()
