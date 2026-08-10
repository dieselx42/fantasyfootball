"""Sleeper adapter.

Sleeper's read API needs no authentication and no API key, which makes it the
most useful source available on day one — even if your league is on Yahoo, this
adapter can supply the player universe (names, teams, byes, injury status) that
a projections CSV is missing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..players import Player, make_player_id, normalize_pos, normalize_team
from .base import PlatformAdapter, PlatformError

API = "https://api.sleeper.app/v1"
TIMEOUT = 25


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


class SleeperAdapter(PlatformAdapter):
    kind = "sleeper"
    label = "Sleeper"
    description = "Free public API, no login required. Also usable as a player-data source for any league."
    requires_auth = False
    setup_fields = (
        ("league_id", "Sleeper league ID", "The number in your league URL."),
    )

    def status(self) -> dict[str, Any]:
        try:
            _get("/state/nfl")
        except PlatformError as exc:
            return {"kind": self.kind, "ready": False, "detail": str(exc)}
        return {"kind": self.kind, "ready": True, "detail": "Public API reachable."}

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
        rosters = _get(f"/league/{league_id}/rosters")
        return {
            str(r.get("roster_id")): list(r.get("players") or []) for r in rosters
        }
