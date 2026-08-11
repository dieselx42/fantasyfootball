"""Yahoo Fantasy Sports adapter.

Yahoo's API is OAuth2-only: there is no anonymous read path, so this adapter
needs an app registered at https://developer.yahoo.com/apps/ (free). The token
exchange and refresh are implemented here; the one thing this code cannot do
for you is click "Allow" in a browser.

Setup, once:

  1. Create an app at https://developer.yahoo.com/apps/
     - OAuth Client Type: **Confidential Client** (this app holds a secret)
     - Redirect URI: ``https://localhost:8000`` — nothing needs to listen
       there, see :data:`DEFAULT_REDIRECT_URI`
     - API Permissions: Fantasy Sports read, if the form offers it. Newer
       app forms do not list Fantasy Sports separately.
  2. Paste the Client ID and Client Secret into the setup wizard, along with
     the same Redirect URI you registered.
  3. Open the authorize URL it gives you, approve, and paste the code back.

Yahoo's app form has changed more than once, and the fields it offers differ
by account. Whatever it asks for, the rule that matters is that the Redirect
URI here and the one registered there are byte-identical; a mismatch fails the
token exchange with an error that does not say so.

Credentials go to the storage backend's secret store, never into a league
config — league configs are meant to be committed and shared. Tokens go to the
same place: a gitignored file under ``secrets/`` locally, a private row when
hosted.
"""

from __future__ import annotations

import base64
import json
import re
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
TIMEOUT = 25

#: Where Yahoo sends you after you approve. This must match the Redirect URI
#: registered on the Yahoo app *exactly*, or the token exchange fails with an
#: unhelpful error, so it is configurable rather than assumed.
#:
#: Yahoo's app form used to offer an "Installed Application" type whose
#: redirect was the literal string ``oob`` — Yahoo displayed the code on screen
#: for you to copy. Newer app forms ask for a real URI instead. A localhost URL
#: works without running anything there: Yahoo redirects the browser to
#: ``https://localhost:8000/?code=…``, the page fails to load because nothing
#: is listening, and the code is sitting in the address bar to copy. Same
#: copy-paste flow, on a form that will accept it.
DEFAULT_REDIRECT_URI = "https://localhost:8000"

#: Yahoo's game key for NFL changes each season; ``nfl`` resolves to current.
GAME_KEY = "nfl"


def extract_code(raw: str) -> str:
    """The authorization code, however it was pasted.

    The code arrives in a browser address bar, so what lands in the box is
    whatever was convenient to select: the whole redirect URL, the query
    string, ``code=abc123``, or the bare value. All of them mean the same
    thing, and Yahoo's rejection of the wrong one is an opaque
    ``invalid_grant`` — so they are all accepted here rather than left as a
    trap at the last step of setup.
    """
    text = (raw or "").strip().strip("?&")
    if not text:
        return ""
    if "code=" in text:
        text = text.split("code=", 1)[1]
    # Drop anything Yahoo appended after the code.
    for separator in ("&", "#", " "):
        text = text.split(separator, 1)[0]
    return urllib.parse.unquote(text).strip()


class YahooAdapter(PlatformAdapter):
    kind = "yahoo"
    label = "Yahoo Fantasy"
    description = "Requires a free Yahoo developer app for API access."
    requires_auth = True
    setup_fields = (
        ("client_id", "Yahoo Client ID", "From https://developer.yahoo.com/apps/"),
        ("client_secret", "Yahoo Client Secret", "Kept in the secret store, never in a league file."),
        ("league_id", "League ID", "The number in your league URL, e.g. 123456."),
        ("redirect_uri", "Redirect URI",
         "Must match your Yahoo app exactly. Default https://localhost:8000; "
         "older apps used the literal word oob."),
    )

    @property
    def redirect_uri(self) -> str:
        return str(self.settings.get("redirect_uri") or DEFAULT_REDIRECT_URI)

    def capabilities(self) -> set[str]:
        return {
            "league", "rules", "players", "rosters",
            "scoreboard", "transactions", "draft",
        }

    # -- auth --------------------------------------------------------------

    def authorize_url(self) -> str:
        client_id = self.credential("client_id")
        if not client_id:
            raise PlatformError("Set your Yahoo Client ID first.")
        query = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": self.redirect_uri,
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
                "redirect_uri": self.redirect_uri,
                "code": extract_code(code),
            }
        )
        self._save_token(token)
        return {"ok": True, "expires_in": token.get("expires_in")}

    def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        client_id = self.credential("client_id")
        client_secret = self.credential("client_secret")
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
                    "redirect_uri": self.redirect_uri,
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
        if not self.credential("client_id"):
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

    # -- rules -------------------------------------------------------------

    def fetch_rules(self, league_id: str) -> dict[str, Any]:
        """Read the league's real rules out of Yahoo and into our config shape.

        Scoring is mapped by stat *name*, not by stat id. Yahoo identifies
        stats numerically in ``stat_modifiers``, and those ids are stable but
        undocumented — hardcoding a table of them means that the day one is
        wrong, every valuation in the app is quietly wrong with nothing on
        screen to say so. So this reads ``/game/nfl/stat_categories`` for the
        id-to-name map and matches on the names, which are self-describing and
        verifiable against what you see in Yahoo's own settings page.

        Anything that does not map comes back in ``unmapped`` rather than
        being dropped.
        """
        key = self._league_key(league_id)
        categories = _parse_stat_categories(self._get(f"/game/{GAME_KEY}/stat_categories"))
        settings_payload = self._get(f"/league/{key}/settings")
        node = _first_with(settings_payload, "stat_modifiers") or {}

        scoring: dict[str, float] = {}
        unmapped: list[dict[str, Any]] = []
        # Points allowed arrives as one stat per band; collected here and
        # folded into a single banded rule below.
        bands: list[tuple[int | None, float]] = []

        for stat_id, value in _iter_stat_modifiers(node.get("stat_modifiers")):
            category = categories.get(stat_id)
            if category is None:
                unmapped.append({"stat_id": stat_id, "name": "", "value": value,
                                 "why": "Yahoo did not describe this stat id."})
                continue

            band = points_allowed_band(category["name"])
            if band is not None:
                bands.append((band[0], value))
                continue

            our_key = map_stat_name(category["name"], category.get("position_type", ""))
            if our_key is None:
                unmapped.append({"stat_id": stat_id, "name": category["name"],
                                 "value": value,
                                 "why": "No equivalent in this app's stat vocabulary."})
                continue
            scoring[our_key] = value

        rules: dict[str, Any] = {"scoring": scoring, "unmapped": unmapped}
        if bands:
            rules["scoring_bands"] = {"dst_pa": _order_bands(bands)}

        roster = _parse_roster_positions(settings_payload)
        if roster["slots"]:
            rules["roster"] = roster
        league_node = _first_with(settings_payload, "num_teams") or {}
        if league_node.get("num_teams"):
            rules["team_count"] = int(league_node["num_teams"])

        playoffs = _parse_playoffs(settings_payload)
        if playoffs:
            rules["playoffs"] = playoffs
        waivers = _parse_waivers(settings_payload)
        if waivers:
            rules["waivers"] = waivers

        return rules

    # -- in-season ---------------------------------------------------------

    def fetch_scoreboard(self, league_id: str, week: int) -> dict[str, Any]:
        key = self._league_key(league_id)
        payload = self._get(f"/league/{key}/scoreboard;week={int(week)}")
        return _parse_scoreboard(payload, int(week))

    def fetch_transactions(self, league_id: str) -> list[dict[str, Any]]:
        key = self._league_key(league_id)
        payload = self._get(f"/league/{key}/transactions")
        return _parse_transactions(payload)

    def fetch_draft_results(self, league_id: str) -> list[dict[str, Any]]:
        key = self._league_key(league_id)
        results = _parse_draft_results(self._get(f"/league/{key}/draftresults"))
        if not results:
            return []

        # Draft results carry player *keys* only, so every pick would read
        # "nfl.p.12345" without a second lookup. Yahoo can return a specific
        # set of players in one call, so ask for exactly the keys drafted
        # rather than walking the whole player universe.
        wanted = [p["player_key"] for p in results if p.get("player_key")]
        lookup = self._players_by_key(key, wanted)
        for pick in results:
            found = lookup.get(pick.get("player_key", ""))
            if found:
                pick.update(found)
        return results

    def _players_by_key(
        self, league_key: str, player_keys: list[str]
    ) -> dict[str, dict[str, str]]:
        """Resolve Yahoo player keys to names and positions, 25 at a time."""
        out: dict[str, dict[str, str]] = {}
        for start in range(0, len(player_keys), 25):
            chunk = player_keys[start:start + 25]
            if not chunk:
                continue
            payload = self._get(
                f"/league/{league_key}/players;player_keys={','.join(chunk)}"
            )
            out.update(_parse_player_keys(payload))
        return out


# --------------------------------------------------------------------------
# Yahoo's JSON is XML translated literally, and that has one consequence that
# dominates every parser below: **a single object's fields are split across a
# list of one-key dicts.** A player comes back as
#
#     {"player": [[{"player_key": "nfl.p.1"}, {"name": {"full": "..."}}, ...]]}
#
# not as one dict with both fields. So a predicate like
# ``"player_key" in node and "name" in node`` never matches anything, however
# defensively the payload is walked. Repeated elements are then keyed by
# stringified index ("0", "1", …) alongside a "count", rather than being a
# plain list.
#
# :func:`_merge` puts one object back together and :func:`_elements` finds
# every occurrence of a named object, merged. Everything else reads normally.
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


def _merge(node: Any) -> dict[str, Any]:
    """Reassemble one Yahoo object from its split-across-a-list form.

    Handles the dict case (already whole), the list-of-one-key-dicts case, and
    the list-containing-that-list case Yahoo uses for players and teams. Only
    descends through those wrapper lists, never into a child object's own
    fields, so sibling objects can't bleed into each other.

    Note that ``count`` is kept. Yahoo uses that word twice — as the length
    marker on a collection, and as a real field on a roster position ("how
    many QB slots"). Dropping it loses the roster shape, and this only ever
    merges a single element, never a collection, so the marker never appears.
    """
    if isinstance(node, dict):
        return dict(node)
    if not isinstance(node, list):
        return {}

    out: dict[str, Any] = {}
    for item in node:
        if isinstance(item, dict):
            for key, value in item.items():
                out.setdefault(key, value)
        elif isinstance(item, list):
            for sub in item:
                if isinstance(sub, dict):
                    for key, value in sub.items():
                        out.setdefault(key, value)
    return out


def _elements(payload: Any, name: str) -> list[dict[str, Any]]:
    """Every ``name`` object in the payload, each merged into one dict.

    ``_elements(payload, "player")`` returns whole players regardless of how
    deeply Yahoo buried them or which endpoint they came from.
    """
    out: list[dict[str, Any]] = []
    for node in _walk(payload):
        if name not in node:
            continue
        merged = _merge(node[name])
        if merged:
            out.append(merged)
    return out


def _full_name(node: dict[str, Any]) -> str:
    name = node.get("name")
    if isinstance(name, dict):
        return str(name.get("full") or "").strip()
    return str(name or "").strip()


def _parse_league_teams(payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    league_node = _first_with(payload, "league_key", "name") or {}
    league = {
        "name": league_node.get("name", "Yahoo League"),
        "season": int(league_node.get("season") or 0) or None,
    }

    teams: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in _elements(payload, "team"):
        team_key = str(node.get("team_key") or "")
        name = _full_name(node)
        if not team_key or not name or team_key in seen:
            continue
        seen.add(team_key)
        found = _first_with(node.get("managers"), "nickname") if node.get("managers") else None
        teams.append(
            {
                "id": _team_id(team_key),
                "name": name,
                "manager": (found or {}).get("nickname", ""),
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
    """``team_id`` -> our player ids.

    Each team is parsed with its own roster rather than by tracking a "current
    team" while walking, because the flat-walk version silently attributed
    players to whichever team happened to appear before them.
    """
    rosters: dict[str, list[str]] = {}
    for team in _elements(payload, "team"):
        team_key = str(team.get("team_key") or "")
        if not team_key:
            continue
        team_id = _team_id(team_key)
        roster = rosters.setdefault(team_id, [])
        for node in _elements(team.get("roster"), "player"):
            full = _full_name(node)
            if not full:
                continue
            pos = normalize_pos(node.get("display_position") or "")
            team_abbr = normalize_team(node.get("editorial_team_abbr") or "")
            roster.append(make_player_id(full, pos, team_abbr))
    return rosters


def _parse_players(payload: Any) -> list[Player]:
    players: list[Player] = []
    seen: set[str] = set()
    for node in _elements(payload, "player"):
        key = str(node.get("player_key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        full = _full_name(node)
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


def _parse_player_keys(payload: Any) -> dict[str, dict[str, str]]:
    """``player_key`` -> name/pos/team, for resolving draft picks."""
    out: dict[str, dict[str, str]] = {}
    for node in _elements(payload, "player"):
        key = str(node.get("player_key") or "")
        full = _full_name(node)
        if not key or not full:
            continue
        out[key] = {
            "name": full,
            "pos": normalize_pos(node.get("display_position") or ""),
            "team": normalize_team(node.get("editorial_team_abbr") or ""),
        }
    return out


# --------------------------------------------------------------------------
# Rules: turning Yahoo's settings into this app's config
#
# Yahoo identifies stats by number. The numbers are stable but undocumented,
# so this maps on the *names* Yahoo itself publishes at
# /game/nfl/stat_categories — the same words you see on the league settings
# page. A name this app does not recognise is reported, never guessed at.
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Lowercase, drop punctuation, collapse spaces. '50+' keeps its plus."""
    cleaned = re.sub(r"[^a-z0-9+ ]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


#: Yahoo stat name -> our stat key, for offence and kicking.
STAT_NAMES: dict[str, str] = {
    "passing yards": "pass_yd",
    "passing touchdowns": "pass_td",
    "passing attempts": "pass_att",
    "completions": "pass_cmp",
    "incomplete passes": "pass_inc",
    # A quarterback's interception, not a defence's — see DEFENSE_STAT_NAMES
    # for the same word on the other side of the ball.
    "interceptions": "pass_int",
    "interception": "pass_int",
    "sacks": "pass_sacked",
    "sacked": "pass_sacked",
    "rushing yards": "rush_yd",
    "rushing touchdowns": "rush_td",
    "rushing attempts": "rush_att",
    "receptions": "rec",
    "reception yards": "rec_yd",
    "receiving yards": "rec_yd",
    "reception touchdowns": "rec_td",
    "receiving touchdowns": "rec_td",
    "targets": "rec_tgt",
    "return touchdowns": "ret_td",
    "offensive fumble return td": "off_fum_ret_td",
    "offensive fumble return touchdowns": "off_fum_ret_td",
    "2 point conversions": "two_pt",
    "fumbles lost": "fum_lost",
    "fumbles": "fum",
    "field goals 0 19 yards": "fg_0_19",
    "field goals 20 29 yards": "fg_20_29",
    "field goals 30 39 yards": "fg_30_39",
    "field goals 40 49 yards": "fg_40_49",
    "field goals 50+ yards": "fg_50p",
    "field goals missed 0 19 yards": "fg_miss_0_19",
    "field goals missed 20 29 yards": "fg_miss_20_29",
    "field goals missed 30 39 yards": "fg_miss_30_39",
    "field goals missed 40 49 yards": "fg_miss_40_49",
    "field goals missed 50+ yards": "fg_miss_50p",
    "field goals missed": "fg_miss",
    "point after attempt made": "xp_made",
    "point after attempt missed": "xp_miss",
}

#: The same exercise for defence/special teams, kept separate because Yahoo
#: reuses words: "Interceptions" is a quarterback's mistake on one side of the
#: ball and a defence's takeaway on the other.
DEFENSE_STAT_NAMES: dict[str, str] = {
    "sack": "dst_sack",
    "sacks": "dst_sack",
    "interception": "dst_int",
    "interceptions": "dst_int",
    "fumble recovery": "dst_fum_rec",
    "fumble recoveries": "dst_fum_rec",
    "touchdown": "dst_td",
    "touchdowns": "dst_td",
    "defensive touchdowns": "dst_td",
    "safety": "dst_safety",
    "safeties": "dst_safety",
    "block kick": "dst_block",
    "blocked kick": "dst_block",
    "kickoff and punt return touchdowns": "dst_ret_td",
    "return touchdowns": "dst_ret_td",
    "extra point returned": "dst_xp_ret",
    "yards allowed": "dst_ya",
}

#: Yahoo scores defensive points allowed as a set of *separate* stats, one per
#: band ("Points Allowed 0 points", "Points Allowed 1-6 points", …). Those
#: become a single banded rule here rather than seven multipliers.
POINTS_ALLOWED = re.compile(r"^points allowed (\d+)(?: (\d+))?(\+)? points?$")


def map_stat_name(name: str, position_type: str = "") -> str | None:
    """Our stat key for a Yahoo stat name, or None if we have no equivalent."""
    key = _norm(name)
    if position_type.upper() in ("DT", "D", "DEF"):
        return DEFENSE_STAT_NAMES.get(key)
    return STAT_NAMES.get(key)


def points_allowed_band(name: str) -> tuple[int | None, bool] | None:
    """Parse a 'Points Allowed 1-6 points' style name into its upper bound.

    Returns ``(max, is_open_ended)``, where an open-ended band ("35+ points")
    has ``max`` of None.
    """
    match = POINTS_ALLOWED.match(_norm(name))
    if not match:
        return None
    low, high, plus = match.group(1), match.group(2), match.group(3)
    if plus:
        return (None, True)
    return (int(high if high else low), False)


def _order_bands(bands: list[tuple[int | None, float]]) -> list[dict[str, Any]]:
    """Sort points-allowed bands ascending, open-ended one last.

    ``ff.config.validate`` rejects a band list that does not end open-ended,
    since otherwise some scores would silently count for nothing — so the
    open-ended band is placed last explicitly rather than by luck of sorting.
    """
    closed = sorted(
        ((ceiling, points) for ceiling, points in bands if ceiling is not None),
        key=lambda row: row[0],
    )
    ordered = [{"max": ceiling, "points": points} for ceiling, points in closed]
    open_ended = [points for ceiling, points in bands if ceiling is None]
    ordered.append({"max": None, "points": open_ended[0] if open_ended else 0})
    return ordered


def _parse_stat_categories(payload: Any) -> dict[str, dict[str, str]]:
    """``stat_id`` -> {name, position_type} from /game/nfl/stat_categories."""
    out: dict[str, dict[str, str]] = {}
    for node in _elements(payload, "stat"):
        if "stat_id" not in node or not (node.get("name") or node.get("display_name")):
            continue
        stat_id = str(node["stat_id"])
        if stat_id in out:
            continue
        position_type = ""
        types = node.get("position_types")
        if types:
            found = _first_with(types, "position_type")
            position_type = str((found or {}).get("position_type", ""))
        out[stat_id] = {
            "name": str(node.get("name") or node.get("display_name") or ""),
            "position_type": position_type,
        }
    return out


def _iter_stat_modifiers(node: Any) -> list[tuple[str, float]]:
    """``[(stat_id, value)]`` from a league's stat_modifiers block."""
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for entry in _elements(node, "stat"):
        if "stat_id" not in entry or "value" not in entry:
            continue
        stat_id = str(entry["stat_id"])
        if stat_id in seen:
            continue
        try:
            value = float(entry["value"])
        except (TypeError, ValueError):
            continue
        seen.add(stat_id)
        out.append((stat_id, value))
    return out


#: Yahoo roster position -> the positions eligible for that slot.
ROSTER_SLOTS: dict[str, list[str]] = {
    "QB": ["QB"], "RB": ["RB"], "WR": ["WR"], "TE": ["TE"], "K": ["K"],
    "DEF": ["DST"], "D": ["DST"], "DST": ["DST"],
    "W/R": ["WR", "RB"],
    "W/T": ["WR", "TE"],
    "R/T": ["RB", "TE"],
    "W/R/T": ["RB", "WR", "TE"],
    "Q/W/R/T": ["QB", "RB", "WR", "TE"],
}


def _parse_roster_positions(payload: Any) -> dict[str, Any]:
    slots: list[dict[str, Any]] = []
    bench = 0
    ir = 0
    seen: set[str] = set()

    for node in _elements(payload, "roster_position"):
        if "position" not in node or "count" not in node:
            continue
        position = str(node["position"]).upper()
        try:
            count = int(node["count"])
        except (TypeError, ValueError):
            continue
        if position in seen:
            continue
        seen.add(position)

        if position in ("BN", "BENCH"):
            bench = count
            continue
        if position in ("IR", "IR+", "IL"):
            ir = count
            continue
        eligible = ROSTER_SLOTS.get(position)
        if not eligible:
            continue
        # Yahoo calls the defence slot DEF; everything in this app calls it
        # DST, and a slot label that disagrees with the position it holds is
        # a confusing thing to show in the roster editor.
        label = "DST" if position in ("DEF", "D") else position
        slots.append({"slot": label, "count": count, "eligible": eligible})

    return {"slots": slots, "bench": bench, "ir": ir}


def _parse_playoffs(payload: Any) -> dict[str, Any]:
    node = _first_with(payload, "num_playoff_teams") or {}
    if not node:
        return {}
    out: dict[str, Any] = {}
    try:
        out["teams"] = int(node["num_playoff_teams"])
    except (TypeError, ValueError):
        return {}
    start = node.get("playoff_start_week")
    end = node.get("end_week")
    try:
        first, last = int(start), int(end)
        if 0 < first <= last:
            out["weeks"] = list(range(first, last + 1))
    except (TypeError, ValueError):
        pass
    return out


#: Yahoo's waiver settings vocabulary, mapped to ours.
WAIVER_TYPES = {
    "FR": "continual_rolling",     # free-for-all after the waiver period
    "CR": "continual_rolling",
    "R": "reverse_standings",
    "S": "reverse_standings",
}


def _parse_waivers(payload: Any) -> dict[str, Any]:
    node = _first_with(payload, "waiver_type") or {}
    if not node:
        return {}
    out: dict[str, Any] = {}
    if node.get("uses_faab") in (1, "1", True, "true"):
        out["type"] = "faab"
    else:
        out["type"] = WAIVER_TYPES.get(str(node.get("waiver_type") or ""), "continual_rolling")
    try:
        out["waiver_days"] = int(node["waiver_time"])
    except (TypeError, ValueError, KeyError):
        pass
    if node.get("waiver_rule"):
        out["process"] = str(node["waiver_rule"])
    return out


# --------------------------------------------------------------------------
# In-season payloads
# --------------------------------------------------------------------------

def _team_id(team_key: str) -> str:
    return str(team_key).rsplit(".", 1)[-1]


def _parse_scoreboard(payload: Any, week: int) -> dict[str, Any]:
    """Fixtures and points for one week.

    Yahoo nests each matchup's two teams in a numbered dict, so the teams are
    collected per matchup node rather than globally — pairing them by document
    order would silently invent fixtures if a single team failed to parse.
    """
    games: list[dict[str, str]] = []
    scores: dict[str, float] = {}
    finals: list[bool] = []

    for matchup in _elements(payload, "matchup"):
        if "teams" not in matchup:
            continue
        sides: list[tuple[str, float | None]] = []
        for node in _elements(matchup["teams"], "team"):
            team_key = str(node.get("team_key") or "")
            if not team_key:
                continue
            team_id = _team_id(team_key)
            if any(team_id == existing for existing, _ in sides):
                continue
            points = None
            total = (node.get("team_points") or {}).get("total")
            try:
                points = float(total)
            except (TypeError, ValueError):
                points = None
            sides.append((team_id, points))

        if len(sides) != 2:
            continue
        (home, home_pts), (away, away_pts) = sides
        games.append({"home": home, "away": away})
        if home_pts is not None:
            scores[home] = home_pts
        if away_pts is not None:
            scores[away] = away_pts
        finals.append(str(matchup.get("status") or "") == "postevent")

    return {
        "week": week,
        "games": games,
        "scores": scores,
        "final": bool(finals) and all(finals),
    }


#: Yahoo transaction type -> ours.
TRANSACTION_TYPES = {
    "add": "add",
    "drop": "drop",
    "add/drop": "add_drop",
    "trade": "trade",
    "commish": "add_drop",
}


def _parse_transactions(payload: Any) -> list[dict[str, Any]]:
    """Roster moves, with players named rather than keyed.

    Names are deliberate: the sync layer matches them against the loaded
    projection pool, so an adapter never has to know this app's player ids.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for node in _elements(payload, "transaction"):
        key = str(node.get("transaction_key") or "")
        if not key or "type" not in node or key in seen:
            continue
        seen.add(key)

        kind = TRANSACTION_TYPES.get(str(node["type"]).lower())
        if kind is None:
            continue
        # A vetoed or pending claim is in the feed but never happened; taking
        # it as real would put a player on a roster he was never on.
        if str(node.get("status") or "successful") != "successful":
            continue

        adds: list[dict[str, str]] = []
        drops: list[dict[str, str]] = []
        teams: set[str] = set()
        destination: str | None = None
        source: str | None = None

        for player in _elements(node.get("players"), "player"):
            data = player.get("transaction_data")
            if data is None:
                continue
            # Yahoo gives transaction_data as a bare dict on some moves and a
            # single-element list on others.
            entry = _merge(data)
            full = _full_name(player)
            if not full:
                continue
            # Carry position and team alongside the name. Our player ids are
            # derived from name plus position, so handing the sync layer all
            # three lets it reconstruct an exact id instead of guessing from a
            # name that two players might share.
            reference = {
                "name": full,
                "pos": normalize_pos(player.get("display_position") or ""),
                "team": normalize_team(player.get("editorial_team_abbr") or ""),
            }
            movement = str(entry.get("type") or "")
            if movement == "add":
                adds.append(reference)
                if entry.get("destination_team_key"):
                    destination = _team_id(entry["destination_team_key"])
            elif movement == "drop":
                drops.append(reference)
                if entry.get("source_team_key"):
                    source = _team_id(entry["source_team_key"])
            for side in ("source_team_key", "destination_team_key"):
                if entry.get(side):
                    teams.add(_team_id(entry[side]))

        team_id = destination or source or (sorted(teams)[0] if teams else "")
        partner = None
        if kind == "trade":
            others = sorted(t for t in teams if t != team_id)
            partner = others[0] if others else None

        faab = None
        if node.get("faab_bid") not in (None, ""):
            try:
                faab = float(node["faab_bid"])
            except (TypeError, ValueError):
                faab = None

        out.append(
            {
                "type": kind,
                "team_id": team_id,
                "partner_team_id": partner,
                "adds": adds,
                "drops": drops,
                "faab": faab,
                "timestamp": node.get("timestamp"),
                "external_id": key,
            }
        )

    out.sort(key=lambda t: int(t.get("timestamp") or 0))
    return out


def _parse_draft_results(payload: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for node in _elements(payload, "draft_result"):
        if "pick" not in node or "round" not in node:
            continue
        try:
            pick = int(node["pick"])
            rnd = int(node["round"])
        except (TypeError, ValueError):
            continue
        if pick in seen:
            continue
        seen.add(pick)
        price = None
        if node.get("cost") not in (None, ""):
            try:
                price = float(node["cost"])
            except (TypeError, ValueError):
                price = None
        out.append(
            {
                "pick": pick,
                "round": rnd,
                "team_id": _team_id(node.get("team_key", "")),
                "player_key": str(node.get("player_key") or ""),
                "name": "",
                "pos": "",
                "team": "",
                "price": price,
            }
        )
    out.sort(key=lambda p: p["pick"])
    return out
