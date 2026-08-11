"""ESPN adapter.

ESPN has no public/official fantasy API. Public leagues are readable without
credentials; private leagues require the ``espn_s2`` and ``SWID`` cookies from
a logged-in browser session.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..players import Player, make_player_id, normalize_pos
from .base import PlatformAdapter, PlatformError

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"
TIMEOUT = 25

#: ESPN encodes positions as integers.
POSITION_BY_ID = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}


class EspnAdapter(PlatformAdapter):
    kind = "espn"
    label = "ESPN"
    description = "Public leagues work as-is; private leagues need browser cookies."
    requires_auth = False
    setup_fields = (
        ("league_id", "League ID", "The number in your league URL."),
        ("season", "Season", "e.g. 2025"),
        ("espn_s2", "espn_s2 cookie", "Private leagues only."),
        ("swid", "SWID cookie", "Private leagues only, including the braces."),
    )

    def capabilities(self) -> set[str]:
        # Public leagues read fine; private ones need browser cookies. The
        # in-season endpoints are not wired up yet, and saying so is better
        # than offering a button that fails.
        return {"league", "rosters", "players"}

    def _cookies(self) -> str:
        # Session cookies are credentials, so they come from the secret store
        # rather than the league config. See ff.config.CREDENTIAL_KEYS.
        s2 = self.credential("espn_s2")
        swid = self.credential("swid")
        parts = []
        if s2:
            parts.append(f"espn_s2={s2}")
        if swid:
            parts.append(f"SWID={swid}")
        return "; ".join(parts)

    def _get(self, path: str) -> Any:
        headers = {"User-Agent": "ff-assistant/1.0"}
        cookies = self._cookies()
        if cookies:
            headers["Cookie"] = cookies
        request = urllib.request.Request(f"{BASE}{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PlatformError(
                    "ESPN denied access — this looks like a private league. "
                    "Add your espn_s2 and SWID cookies."
                ) from exc
            raise PlatformError(f"ESPN returned HTTP {exc.code}.") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PlatformError(f"Could not reach ESPN: {exc}") from exc

    def status(self) -> dict[str, Any]:
        if not self.settings.get("league_id"):
            return {"kind": self.kind, "ready": False, "detail": "Add your league ID."}
        return {"kind": self.kind, "ready": True, "detail": "Ready to try."}

    def fetch_league(self, league_id: str) -> dict[str, Any]:
        season = self.settings.get("season") or 2025
        payload = self._get(f"/{season}/segments/0/leagues/{league_id}?view=mTeam")
        teams = []
        for team in payload.get("teams", []):
            name = team.get("name") or (
                f"{team.get('location', '')} {team.get('nickname', '')}".strip()
            )
            teams.append(
                {"id": str(team.get("id")), "name": name or "Team", "manager": ""}
            )
        return {
            "name": (payload.get("settings") or {}).get("name", "ESPN League"),
            "season": int(season),
            "team_count": len(teams),
            "teams": teams,
        }

    def fetch_rosters(self, league_id: str) -> dict[str, list[str]]:
        season = self.settings.get("season") or 2025
        payload = self._get(f"/{season}/segments/0/leagues/{league_id}?view=mRoster")
        out: dict[str, list[str]] = {}
        for team in payload.get("teams", []):
            ids = []
            for entry in (team.get("roster") or {}).get("entries", []):
                info = (entry.get("playerPoolEntry") or {}).get("player") or {}
                name = info.get("fullName")
                pos = POSITION_BY_ID.get(info.get("defaultPositionId"), "")
                if name and pos:
                    ids.append(make_player_id(name, normalize_pos(pos), ""))
            out[str(team.get("id"))] = ids
        return out

    def fetch_players(self) -> list[Player]:
        raise PlatformError(
            "ESPN player import is not wired up yet — import a projections CSV."
        )
