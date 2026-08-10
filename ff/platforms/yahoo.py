"""Yahoo Fantasy Sports adapter.

Yahoo's API is OAuth2-only: there is no anonymous read path, so this adapter
needs an app registered at https://developer.yahoo.com/apps/ (free). The token
exchange and refresh are implemented here; the one thing this code cannot do
for you is click "Allow" in a browser.

Setup, once:

  1. Create an app at https://developer.yahoo.com/apps/
     - Application Type: Installed Application
     - Redirect URI: ``oob``
     - API Permissions: Fantasy Sports (Read, or Read/Write to submit trades)
  2. Paste the Client ID and Client Secret into the setup wizard.
  3. Open the authorize URL it gives you, approve, and paste the code back.

Tokens go to the active storage backend: a gitignored file under
``secrets/`` locally, or a database row when hosted.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..players import Player, make_player_id, normalize_pos, normalize_team
from .base import PlatformAdapter, PlatformError

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
REDIRECT_URI = "oob"
TIMEOUT = 25

#: Yahoo's game key for NFL changes each season; ``nfl`` resolves to current.
GAME_KEY = "nfl"


class YahooAdapter(PlatformAdapter):
    kind = "yahoo"
    label = "Yahoo Fantasy"
    description = "Requires a free Yahoo developer app for API access."
    requires_auth = True
    setup_fields = (
        ("client_id", "Yahoo Client ID", "From https://developer.yahoo.com/apps/"),
        ("client_secret", "Yahoo Client Secret", "Kept locally in secrets/, never committed."),
        ("league_id", "League ID", "The number in your league URL, e.g. 123456."),
    )

    # -- auth --------------------------------------------------------------

    def authorize_url(self) -> str:
        client_id = self.settings.get("client_id")
        if not client_id:
            raise PlatformError("Set your Yahoo Client ID first.")
        query = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "language": "en-us",
            }
        )
        return f"{AUTH_URL}?{query}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        """Trade the pasted authorization code for a token pair."""
        token = self._token_request(
            {
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
                "code": code.strip(),
            }
        )
        self._save_token(token)
        return {"ok": True, "expires_in": token.get("expires_in")}

    def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        client_id = self.settings.get("client_id")
        client_secret = self.settings.get("client_secret")
        if not client_id or not client_secret:
            raise PlatformError("Yahoo Client ID and Secret are both required.")

        basic = base64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode()
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode(payload).encode(),
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                token = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise PlatformError(f"Yahoo rejected the token request: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PlatformError(f"Could not reach Yahoo: {exc}") from exc

        token["obtained_at"] = int(time.time())
        return token

    def _save_token(self, token: dict[str, Any]) -> None:
        from ..storage import get_backend

        get_backend().save_token(self.kind, token)

    def _load_token(self) -> dict[str, Any] | None:
        from ..storage import get_backend

        return get_backend().load_token(self.kind)

    def _access_token(self) -> str:
        token = self._load_token()
        if not token:
            raise PlatformError(
                "Not connected to Yahoo yet — finish the authorization step."
            )
        age = int(time.time()) - int(token.get("obtained_at", 0))
        if age > int(token.get("expires_in", 3600)) - 120:
            token = self._token_request(
                {
                    "grant_type": "refresh_token",
                    "redirect_uri": REDIRECT_URI,
                    "refresh_token": token["refresh_token"],
                }
            )
            self._save_token(token)
        return token["access_token"]

    # -- requests ----------------------------------------------------------

    def _get(self, path: str) -> Any:
        url = f"{API_BASE}{path}"
        url += ("&" if "?" in url else "?") + "format=json"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._access_token()}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise PlatformError(f"Yahoo returned HTTP {exc.code} for {path}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PlatformError(f"Could not reach Yahoo: {exc}") from exc

    def status(self) -> dict[str, Any]:
        if not self.settings.get("client_id"):
            return {"kind": self.kind, "ready": False,
                    "detail": "Add your Yahoo Client ID and Secret."}
        if not self._load_token():
            return {"kind": self.kind, "ready": False,
                    "detail": "Authorize the app to finish connecting."}
        try:
            self._access_token()
        except PlatformError as exc:
            return {"kind": self.kind, "ready": False, "detail": str(exc)}
        return {"kind": self.kind, "ready": True, "detail": "Connected."}

    # -- data --------------------------------------------------------------

    def _league_key(self, league_id: str) -> str:
        return f"{GAME_KEY}.l.{league_id}"

    def fetch_league(self, league_id: str) -> dict[str, Any]:
        key = self._league_key(league_id)
        payload = self._get(f"/league/{key}/teams")
        league, teams = _parse_league_teams(payload)
        settings = self._get(f"/league/{key}/settings")
        league["raw_settings"] = _dig_settings(settings)
        league["teams"] = teams
        league["team_count"] = len(teams)
        return league

    def fetch_rosters(self, league_id: str) -> dict[str, list[str]]:
        key = self._league_key(league_id)
        payload = self._get(f"/league/{key}/teams/roster")
        return _parse_rosters(payload)

    def fetch_players(self) -> list[Player]:
        """Yahoo paginates players 25 at a time; walk until a short page."""
        league_id = self.settings.get("league_id")
        if not league_id:
            raise PlatformError("Set your Yahoo league ID first.")
        key = self._league_key(league_id)

        out: list[Player] = []
        start = 0
        while start < 1000:
            payload = self._get(f"/league/{key}/players;start={start};count=25")
            page = _parse_players(payload)
            out.extend(page)
            if len(page) < 25:
                break
            start += 25
        return out


# --------------------------------------------------------------------------
# Yahoo's JSON is XML-shaped: numeric string keys, and lists that mix dicts.
# These helpers walk it defensively rather than trusting any fixed depth.
# --------------------------------------------------------------------------

def _walk(node: Any) -> Any:
    """Yield every dict nested anywhere inside Yahoo's response."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _first_with(node: Any, *keys: str) -> dict[str, Any] | None:
    for candidate in _walk(node):
        if all(key in candidate for key in keys):
            return candidate
    return None


def _parse_league_teams(payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    league_node = _first_with(payload, "league_key", "name") or {}
    league = {
        "name": league_node.get("name", "Yahoo League"),
        "season": int(league_node.get("season") or 0) or None,
    }

    teams: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in _walk(payload):
        if "team_key" not in node or "name" not in node:
            continue
        team_key = str(node["team_key"])
        if team_key in seen:
            continue
        seen.add(team_key)
        manager = ""
        managers = node.get("managers")
        if managers:
            found = _first_with(managers, "nickname")
            manager = (found or {}).get("nickname", "")
        teams.append(
            {
                "id": team_key.rsplit(".", 1)[-1],
                "name": str(node["name"]),
                "manager": manager,
                "platform_key": team_key,
            }
        )
    return league, teams


def _dig_settings(payload: Any) -> dict[str, Any]:
    node = _first_with(payload, "roster_positions") or {}
    stat_node = _first_with(payload, "stat_modifiers") or {}
    return {
        "roster_positions": node.get("roster_positions", []),
        "stat_modifiers": stat_node.get("stat_modifiers", {}),
    }


def _parse_rosters(payload: Any) -> dict[str, list[str]]:
    rosters: dict[str, list[str]] = {}
    current: str | None = None
    for node in _walk(payload):
        if "team_key" in node and "name" in node:
            current = str(node["team_key"]).rsplit(".", 1)[-1]
            rosters.setdefault(current, [])
        elif current and "player_key" in node and "name" in node:
            name = node["name"]
            full = name.get("full") if isinstance(name, dict) else str(name)
            pos = normalize_pos(node.get("display_position") or "")
            team = normalize_team(node.get("editorial_team_abbr") or "")
            rosters[current].append(make_player_id(full or "", pos, team))
    return rosters


def _parse_players(payload: Any) -> list[Player]:
    players: list[Player] = []
    seen: set[str] = set()
    for node in _walk(payload):
        if "player_key" not in node or "name" not in node:
            continue
        key = str(node["player_key"])
        if key in seen:
            continue
        seen.add(key)
        name = node["name"]
        full = name.get("full") if isinstance(name, dict) else str(name)
        pos = normalize_pos(node.get("display_position") or "")
        team = normalize_team(node.get("editorial_team_abbr") or "")
        if not full or pos not in ("QB", "RB", "WR", "TE", "K", "DST"):
            continue
        players.append(
            Player(
                player_id=make_player_id(full, pos, team),
                name=full,
                pos=pos,
                team=team,
                bye=_bye(node),
                source="yahoo",
            )
        )
    return players


def _bye(node: dict[str, Any]) -> int | None:
    byes = node.get("bye_weeks")
    if isinstance(byes, dict) and byes.get("week"):
        try:
            return int(byes["week"])
        except (TypeError, ValueError):
            return None
    return None
