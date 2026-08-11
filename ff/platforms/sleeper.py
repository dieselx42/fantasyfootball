"""Sleeper adapter.

Sleeper's read API needs no authentication and no API key, which makes it the
most useful source available on day one — even if your league is on Yahoo, this
adapter can supply the player universe (names, teams, byes, injury status) that
a projections CSV is missing.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Mapping

from ..players import Player, make_player_id, normalize_pos, normalize_team
from .base import PlatformAdapter, PlatformError

API = "https://api.sleeper.app/v1"
TIMEOUT = 25

#: Sleeper's scoring keys are already snake_case and close to ours, so this is
#: mostly a rename. Anything absent here is reported as unmapped rather than
#: dropped, exactly as on the Yahoo side.
SCORING_KEYS: dict[str, str] = {
    "pass_yd": "pass_yd", "pass_td": "pass_td", "pass_int": "pass_int",
    "pass_2pt": "pass_2pt", "pass_cmp": "pass_cmp", "pass_att": "pass_att",
    "pass_inc": "pass_inc", "pass_sack": "pass_sacked",
    "rush_yd": "rush_yd", "rush_td": "rush_td", "rush_2pt": "rush_2pt",
    "rush_att": "rush_att",
    "rec": "rec", "rec_yd": "rec_yd", "rec_td": "rec_td", "rec_2pt": "rec_2pt",
    "rec_tgt": "rec_tgt",
    "fum_lost": "fum_lost", "fum": "fum",
    "st_td": "ret_td", "fum_rec_td": "off_fum_ret_td",
    "fgm_0_19": "fg_0_19", "fgm_20_29": "fg_20_29", "fgm_30_39": "fg_30_39",
    "fgm_40_49": "fg_40_49", "fgm_50p": "fg_50p",
    "fgmiss": "fg_miss", "fgmiss_0_19": "fg_miss_0_19",
    "fgmiss_20_29": "fg_miss_20_29", "fgmiss_30_39": "fg_miss_30_39",
    "fgmiss_40_49": "fg_miss_40_49", "fgmiss_50p": "fg_miss_50p",
    "xpm": "xp_made", "xpmiss": "xp_miss",
    "sack": "dst_sack", "int": "dst_int", "fum_rec": "dst_fum_rec",
    "def_td": "dst_td", "safe": "dst_safety", "blk_kick": "dst_block",
    "def_st_td": "dst_ret_td", "pts_allow": "dst_pa", "yds_allow": "dst_ya",
}

#: Sleeper roster position -> the positions eligible for that slot.
ROSTER_SLOTS: dict[str, list[str]] = {
    "QB": ["QB"], "RB": ["RB"], "WR": ["WR"], "TE": ["TE"], "K": ["K"],
    "DEF": ["DST"], "DST": ["DST"],
    "FLEX": ["RB", "WR", "TE"],
    "WRRB_FLEX": ["WR", "RB"],
    "REC_FLEX": ["WR", "TE"],
    "SUPER_FLEX": ["QB", "RB", "WR", "TE"],
}

#: "pts_allow_1_6" and friends: one scoring key per band.
POINTS_ALLOWED = re.compile(r"^pts_allow_(\d+)(?:_(\d+))?(p)?$")


def _get(path: str) -> Any:
    url = f"{API}{path}"
    request = urllib.request.Request(url, headers={"User-Agent": "ff-assistant/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PlatformError(f"Sleeper returned HTTP {exc.code} for {path}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PlatformError(f"Could not reach Sleeper: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PlatformError("Sleeper returned a response that was not JSON.") from exc


def _points_allowed_band(key: str) -> int | None | bool:
    """Upper bound of a ``pts_allow_*`` key, ``None`` for the open-ended one.

    Returns ``False`` when the key is not a band at all, so that a real
    ``None`` (the open-ended band) is distinguishable from "no match".
    """
    match = POINTS_ALLOWED.match(key)
    if not match:
        return False
    low, high, plus = match.group(1), match.group(2), match.group(3)
    if plus:
        return None
    return int(high if high else low)


def _reference(index: Mapping[str, dict[str, str]], sleeper_id: str) -> dict[str, str] | None:
    found = index.get(str(sleeper_id))
    if not found:
        return None
    return {"name": found["name"], "pos": found["pos"], "team": found["team"]}


def _parse_transaction(
    row: Mapping[str, Any],
    index: Mapping[str, dict[str, str]],
    rosters: Mapping[str, str],
    week: int,
) -> dict[str, Any] | None:
    """One Sleeper move, in our transaction shape.

    Sleeper's ``adds`` and ``drops`` map a player to the roster that ends up
    with them, which means a trade is expressed entirely through those two
    maps rather than through a from/to pair.
    """
    kind = str(row.get("type") or "")
    adds_raw = row.get("adds") or {}
    drops_raw = row.get("drops") or {}
    roster_ids = [str(r) for r in (row.get("roster_ids") or [])]

    if kind == "trade":
        if len(roster_ids) < 2:
            return None
        team_id, partner = roster_ids[0], roster_ids[1]
        adds = [_reference(index, pid) for pid, r in adds_raw.items() if str(r) == team_id]
        drops = [_reference(index, pid) for pid, r in adds_raw.items() if str(r) == partner]
        our_type = "trade"
    else:
        team_id = roster_ids[0] if roster_ids else ""
        partner = None
        adds = [_reference(index, pid) for pid in adds_raw]
        drops = [_reference(index, pid) for pid in drops_raw]
        if adds and drops:
            our_type = "add_drop"
        elif adds:
            our_type = "waiver" if kind == "waiver" else "add"
        elif drops:
            our_type = "drop"
        else:
            return None

    adds = [a for a in adds if a]
    drops = [d for d in drops if d]
    if not adds and not drops:
        return None
    if team_id and team_id not in rosters:
        return None

    bid = (row.get("settings") or {}).get("waiver_bid")
    try:
        faab = float(bid) if bid not in (None, "") else None
    except (TypeError, ValueError):
        faab = None

    return {
        "type": our_type,
        "team_id": team_id,
        "partner_team_id": partner,
        "adds": adds,
        "drops": drops,
        "faab": faab,
        "week": week,
        "timestamp": row.get("status_updated") or row.get("created"),
        "external_id": str(row.get("transaction_id") or ""),
    }


class SleeperAdapter(PlatformAdapter):
    kind = "sleeper"
    label = "Sleeper"
    description = "Free public API, no login required. Also usable as a player-data source for any league."
    requires_auth = False
    setup_fields = (
        ("league_id", "Sleeper league ID", "The number in your league URL."),
    )

    def __init__(self, settings: Mapping[str, Any] | None = None):
        super().__init__(settings)
        #: The player universe is several megabytes, so it is fetched at most
        #: once per adapter instance and reused by every method that needs to
        #: translate a Sleeper id.
        self._index: dict[str, dict[str, str]] | None = None

    def capabilities(self) -> set[str]:
        # No draft-results support here yet: Sleeper exposes it under a
        # separate draft id rather than the league id, so it is a different
        # lookup rather than a missing endpoint.
        return {"league", "rules", "players", "rosters", "scoreboard", "transactions"}

    def status(self) -> dict[str, Any]:
        try:
            state = _get("/state/nfl")
        except PlatformError as exc:
            return {"kind": self.kind, "ready": False, "detail": str(exc)}
        week = (state or {}).get("week")
        return {
            "kind": self.kind,
            "ready": True,
            "detail": f"Public API reachable (NFL week {week}).",
        }

    def current_week(self) -> int:
        try:
            return int((_get("/state/nfl") or {}).get("week") or 1)
        except (PlatformError, TypeError, ValueError):
            return 1

    def fetch_players(self) -> list[Player]:
        raw = _get("/players/nfl")
        if not isinstance(raw, dict):
            raise PlatformError("Unexpected player payload from Sleeper.")

        players: list[Player] = []
        for entry in raw.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("status") in ("Retired",):
                continue
            pos = normalize_pos(entry.get("position") or "")
            if pos not in ("QB", "RB", "WR", "TE", "K", "DST"):
                continue
            name = entry.get("full_name") or entry.get("last_name") or ""
            if pos == "DST":
                name = entry.get("team") or name
            if not name:
                continue
            team = normalize_team(entry.get("team") or "")
            players.append(
                Player(
                    player_id=make_player_id(name, pos, team),
                    name=name,
                    pos=pos,
                    team=team,
                    bye=None,
                    source="sleeper",
                )
            )
        return players

    def fetch_league(self, league_id: str) -> dict[str, Any]:
        league = _get(f"/league/{league_id}")
        users = _get(f"/league/{league_id}/users")
        rosters = _get(f"/league/{league_id}/rosters")

        owner_names = {u["user_id"]: (u.get("display_name") or "") for u in users}
        team_names = {
            u["user_id"]: ((u.get("metadata") or {}).get("team_name")
                           or u.get("display_name") or "Team")
            for u in users
        }

        teams = []
        for roster in rosters:
            owner = roster.get("owner_id")
            teams.append(
                {
                    "id": str(roster.get("roster_id")),
                    "name": team_names.get(owner, f"Team {roster.get('roster_id')}"),
                    "manager": owner_names.get(owner, ""),
                }
            )

        return {
            "name": league.get("name", "Sleeper League"),
            "season": int(league.get("season") or 0) or None,
            "team_count": len(teams),
            "teams": teams,
            "raw_scoring": league.get("scoring_settings") or {},
            "raw_roster_positions": league.get("roster_positions") or [],
        }

    def fetch_rosters(self, league_id: str) -> dict[str, list[str]]:
        """Rosters in *our* player ids.

        Sleeper answers with its own numeric ids ("4034"), which join to
        nothing in this app — a roster of those matches no projection, so
        every lineup would come back empty. The player universe is fetched
        once and used to translate.
        """
        rosters = _get(f"/league/{league_id}/rosters")
        index = self._player_index()
        return {
            str(r.get("roster_id")): [
                index[str(pid)]["player_id"]
                for pid in (r.get("players") or [])
                if str(pid) in index
            ]
            for r in rosters
        }

    def _player_index(self) -> dict[str, dict[str, str]]:
        """Sleeper player id -> our id, name, position and team. Cached.

        ``/players/nfl`` is a multi-megabyte document, so it is fetched at
        most once per adapter instance.
        """
        if self._index is None:
            raw = _get("/players/nfl")
            index: dict[str, dict[str, str]] = {}
            for sleeper_id, entry in (raw or {}).items():
                if not isinstance(entry, dict):
                    continue
                pos = normalize_pos(entry.get("position") or "")
                if pos not in ("QB", "RB", "WR", "TE", "K", "DST"):
                    continue
                team = normalize_team(entry.get("team") or "")
                # Sleeper names a defence by its team, not by a person.
                name = team if pos == "DST" else (entry.get("full_name") or "")
                if not name:
                    continue
                index[str(sleeper_id)] = {
                    "player_id": make_player_id(name, pos, team),
                    "name": name,
                    "pos": pos,
                    "team": team,
                }
            self._index = index
        return self._index

    # -- rules -------------------------------------------------------------

    def fetch_rules(self, league_id: str) -> dict[str, Any]:
        league = _get(f"/league/{league_id}")
        scoring: dict[str, float] = {}
        bands: list[tuple[int | None, float]] = []
        unmapped: list[dict[str, Any]] = []

        for key, value in (league.get("scoring_settings") or {}).items():
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            band = _points_allowed_band(key)
            if band is not False:
                bands.append((band, value))
                continue
            our_key = SCORING_KEYS.get(key)
            if our_key is None:
                unmapped.append({"name": key, "value": value,
                                 "why": "No equivalent in this app's stat vocabulary."})
                continue
            scoring[our_key] = value

        rules: dict[str, Any] = {"scoring": scoring, "unmapped": unmapped}
        if bands:
            closed = sorted((c, p) for c, p in bands if c is not None)
            ordered = [{"max": c, "points": p} for c, p in closed]
            open_ended = [p for c, p in bands if c is None]
            ordered.append({"max": None, "points": open_ended[0] if open_ended else 0})
            rules["scoring_bands"] = {"dst_pa": ordered}

        slots, bench, ir = [], 0, 0
        for position in league.get("roster_positions") or []:
            position = str(position).upper()
            if position == "BN":
                bench += 1
                continue
            if position in ("IR", "TAXI"):
                ir += 1
                continue
            eligible = ROSTER_SLOTS.get(position)
            if not eligible:
                continue
            existing = next((s for s in slots if s["slot"] == position), None)
            if existing:
                existing["count"] += 1
            else:
                slots.append({"slot": position, "count": 1, "eligible": eligible})
        if slots:
            rules["roster"] = {"slots": slots, "bench": bench, "ir": ir}

        settings = league.get("settings") or {}
        if settings.get("num_teams"):
            rules["team_count"] = int(settings["num_teams"])
        if settings.get("playoff_teams"):
            start = int(settings.get("playoff_week_start") or 15)
            rules["playoffs"] = {
                "teams": int(settings["playoff_teams"]),
                "weeks": list(range(start, start + 3)),
            }
        if settings.get("waiver_type") is not None:
            budget = int(settings.get("waiver_budget") or 0)
            rules["waivers"] = {
                "type": "faab" if budget else "continual_rolling",
                "faab_budget": budget or 100,
            }
        return rules

    # -- in-season ---------------------------------------------------------

    def fetch_scoreboard(self, league_id: str, week: int) -> dict[str, Any]:
        """Sleeper pairs teams by a shared ``matchup_id`` rather than naming
        a home and away side, so the fixtures are reconstructed from that."""
        rows = _get(f"/league/{league_id}/matchups/{int(week)}") or []

        pairs: dict[Any, list[tuple[str, float | None]]] = {}
        scores: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            roster_id = str(row.get("roster_id"))
            points = row.get("points")
            try:
                points = float(points)
            except (TypeError, ValueError):
                points = None
            if points:
                scores[roster_id] = points
            pairs.setdefault(row.get("matchup_id"), []).append((roster_id, points))

        games = []
        for matchup_id, sides in pairs.items():
            if matchup_id is None or len(sides) != 2:
                continue
            games.append({"home": sides[0][0], "away": sides[1][0]})

        return {
            "week": int(week),
            "games": games,
            "scores": scores,
            "final": bool(scores) and len(scores) == len(rows),
        }

    def fetch_transactions(self, league_id: str) -> list[dict[str, Any]]:
        """Every completed move, walked week by week.

        Sleeper scopes transactions to a week, so there is no "all of them"
        endpoint — this walks the season to date.
        """
        index = self._player_index()
        rosters = {
            str(r.get("roster_id")): str(r.get("roster_id"))
            for r in (_get(f"/league/{league_id}/rosters") or [])
        }
        out: list[dict[str, Any]] = []

        for week in range(1, self.current_week() + 1):
            try:
                rows = _get(f"/league/{league_id}/transactions/{week}") or []
            except PlatformError:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("status") or "complete") != "complete":
                    continue
                parsed = _parse_transaction(row, index, rosters, week)
                if parsed:
                    out.append(parsed)

        out.sort(key=lambda t: int(t.get("timestamp") or 0))
        return out
