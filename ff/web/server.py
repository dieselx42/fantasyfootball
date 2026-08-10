"""Local web app: a small stdlib HTTP server and a JSON API.

Deliberately dependency-free. On draft night this needs to start with
``python3 run.py`` on any machine with Python, with no venv, no pip and no
build step between you and the board.
"""

from __future__ import annotations

import json
import re
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .. import config as cfgmod
from .. import store, trades
from ..draft import DraftState, board_summary, recommend
from ..platforms import PlatformError, adapter_for, describe_all, get_adapter
from ..players import PROJECTIONS_DIR, parse_projection_csv
from ..pool import build_pool, index_by_id, invalidate
from ..roster import describe_lineup, optimal_lineup
from ..scoring_vocab import STAT_GROUPS
from ..valuation import depth_warnings, replacement_levels

STATIC_DIR = Path(__file__).resolve().parent / "static"

Route = Callable[["Api", dict[str, str], dict[str, Any]], Any]
_ROUTES: list[tuple[str, re.Pattern[str], Route]] = []


def route(method: str, pattern: str) -> Callable[[Route], Route]:
    regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")

    def decorate(func: Route) -> Route:
        _ROUTES.append((method, regex, func))
        return func

    return decorate


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Api:
    """Request-scoped helpers shared by every route."""

    def __init__(self, query: dict[str, list[str]], body: dict[str, Any]):
        self.query = query
        self.body = body

    def arg(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def load_cfg(self, league_id: str) -> dict[str, Any]:
        try:
            return cfgmod.load(league_id)
        except cfgmod.ConfigError as exc:
            raise ApiError(str(exc), 404) from exc

    def conn(self):
        return store.connect()


# --------------------------------------------------------------------------
# Setup + config
# --------------------------------------------------------------------------

@route("GET", "/api/bootstrap")
def bootstrap(api: Api, _params: dict[str, str], _body: dict[str, Any]) -> Any:
    return {
        "platforms": describe_all(),
        "leagues": cfgmod.list_leagues(),
        "stat_groups": {
            group: [{"key": key, "label": label} for key, label in stats]
            for group, stats in STAT_GROUPS.items()
        },
        "positions": list(cfgmod.POSITIONS),
        "projection_files": [p.name for p in PROJECTIONS_DIR.glob("*.csv")],
    }


@route("POST", "/api/leagues")
def create_league(api: Api, _params: dict[str, str], body: dict[str, Any]) -> Any:
    cfg = cfgmod.new_config(
        name=body.get("name") or "My League",
        platform=body.get("platform") or "manual",
        team_count=int(body.get("team_count") or 12),
        ppr=float(body.get("ppr", 0.5)),
    )
    if body.get("teams"):
        cfg["teams"] = [
            cfgmod.new_team(t.get("name", ""), t.get("manager", ""))
            for t in body["teams"]
        ]
    problems = cfgmod.validate(cfg)
    if problems:
        raise ApiError("; ".join(problems))
    cfgmod.save(cfg)
    return {"league": cfg}


@route("GET", "/api/league/<league_id>")
def get_league(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    return {"league": api.load_cfg(params["league_id"])}


@route("PUT", "/api/league/<league_id>")
def update_league(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    incoming = body.get("league") or {}
    # Identity is not editable here; renaming a league would orphan its state.
    incoming.pop("id", None)
    cfg.update(incoming)
    problems = cfgmod.validate(cfg)
    if problems:
        raise ApiError("; ".join(problems))
    cfgmod.save(cfg)
    invalidate(cfg["id"])
    return {"league": cfg, "ok": True}


@route("POST", "/api/league/<league_id>/validate")
def validate_league(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    cfg = dict(api.load_cfg(params["league_id"]))
    cfg.update(body.get("league") or {})
    return {"problems": cfgmod.validate(cfg)}


# --------------------------------------------------------------------------
# Platform
# --------------------------------------------------------------------------

@route("GET", "/api/league/<league_id>/platform/status")
def platform_status(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    try:
        return adapter_for(cfg).status()
    except PlatformError as exc:
        return {"ready": False, "detail": str(exc)}


@route("POST", "/api/league/<league_id>/platform/import")
def platform_import(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    """Pull teams and managers from the platform into the league config."""
    cfg = api.load_cfg(params["league_id"])
    platform = cfg.get("platform") or {}
    league_key = (platform.get("settings") or {}).get("league_id") or platform.get("league_id")
    if not league_key:
        raise ApiError("Set your platform league ID first.")
    try:
        remote = adapter_for(cfg).fetch_league(str(league_key))
    except PlatformError as exc:
        raise ApiError(str(exc), 502) from exc

    if remote.get("teams"):
        cfg["teams"] = [
            {
                "id": cfgmod.slugify(t.get("name") or t.get("id") or "team"),
                "name": t.get("name", "Team"),
                "manager": t.get("manager", ""),
            }
            for t in remote["teams"]
        ]
        cfg["team_count"] = len(cfg["teams"])
    if remote.get("name"):
        cfg["name"] = remote["name"]
    if remote.get("season"):
        cfg["season"] = remote["season"]

    problems = cfgmod.validate(cfg)
    if problems:
        raise ApiError("Imported league did not validate: " + "; ".join(problems))
    cfgmod.save(cfg)
    return {"league": cfg, "imported": len(cfg.get("teams") or [])}


@route("POST", "/api/yahoo/authorize-url")
def yahoo_authorize_url(api: Api, _params: dict[str, str], body: dict[str, Any]) -> Any:
    adapter = get_adapter("yahoo", body.get("settings") or {})
    try:
        return {"url": adapter.authorize_url()}   # type: ignore[attr-defined]
    except PlatformError as exc:
        raise ApiError(str(exc)) from exc


@route("POST", "/api/yahoo/exchange")
def yahoo_exchange(api: Api, _params: dict[str, str], body: dict[str, Any]) -> Any:
    adapter = get_adapter("yahoo", body.get("settings") or {})
    code = (body.get("code") or "").strip()
    if not code:
        raise ApiError("Paste the code Yahoo gave you.")
    try:
        return adapter.exchange_code(code)        # type: ignore[attr-defined]
    except PlatformError as exc:
        raise ApiError(str(exc), 502) from exc


# --------------------------------------------------------------------------
# Players / projections
# --------------------------------------------------------------------------

@route("POST", "/api/projections/import")
def import_projections(api: Api, _params: dict[str, str], body: dict[str, Any]) -> Any:
    text = body.get("csv") or ""
    filename = (body.get("filename") or "import.csv").strip()
    if not text.strip():
        raise ApiError("No CSV content received.")
    if not filename.endswith(".csv"):
        filename += ".csv"
    filename = Path(filename).name          # never escape the projections dir

    parsed = parse_projection_csv(text, Path(filename).stem)
    if not parsed:
        raise ApiError(
            "Could not find any players in that file. It needs a header row "
            "with at least a player-name column."
        )

    PROJECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    (PROJECTIONS_DIR / filename).write_text(text, encoding="utf-8")
    invalidate()

    with_stats = sum(1 for p in parsed if p.stats)
    return {
        "imported": len(parsed),
        "with_stats": with_stats,
        "filename": filename,
        "sample": [p.to_dict() for p in parsed[:5]],
    }


@route("GET", "/api/projections")
def list_projections(api: Api, _params: dict[str, str], _body: dict[str, Any]) -> Any:
    files = []
    for path in sorted(PROJECTIONS_DIR.glob("*.csv")):
        files.append({"name": path.name, "size": path.stat().st_size})
    return {"files": files}


@route("DELETE", "/api/projections/<name>")
def delete_projections(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    path = PROJECTIONS_DIR / Path(params["name"]).name
    if not path.exists():
        raise ApiError("No such projection file.", 404)
    path.unlink()
    invalidate()
    return {"ok": True}


@route("GET", "/api/league/<league_id>/players")
def league_players(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    players = build_pool(cfg)
    pos = (api.arg("pos") or "").upper()
    search = (api.arg("q") or "").lower()
    limit = int(api.arg("limit") or 300)

    rows = players
    if pos and pos != "ALL":
        rows = [p for p in rows if p.pos == pos]
    if search:
        rows = [p for p in rows if search in p.name.lower()]

    return {
        "players": [p.to_dict() for p in rows[:limit]],
        "total": len(players),
        "replacement": {
            k: round(v, 1) for k, v in replacement_levels(players, cfg).items()
        },
        "depth_warnings": depth_warnings(players, cfg),
    }


# --------------------------------------------------------------------------
# Draft
# --------------------------------------------------------------------------

def _draft_state(api: Api, cfg: dict[str, Any]) -> tuple[Any, DraftState]:
    conn = api.conn()
    picks = store.load_picks(conn, cfg["id"])
    return conn, DraftState(cfg, picks)


@route("GET", "/api/league/<league_id>/draft")
def get_draft(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    if not cfg.get("teams"):
        raise ApiError("Add your teams before starting a draft.")
    conn, state = _draft_state(api, cfg)
    pool = build_pool(cfg)
    conn.close()
    return {
        "board": board_summary(state, pool),
        "picks": [p.to_dict() for p in state.slots],
        "teams": cfg.get("teams", []),
        "my_team_id": cfg.get("my_team_id"),
    }


@route("POST", "/api/league/<league_id>/draft/pick")
def make_pick(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    conn, state = _draft_state(api, cfg)
    pool = build_pool(cfg)
    player = index_by_id(pool).get(body.get("player_id", ""))
    if player is None:
        conn.close()
        raise ApiError("That player is not in the pool.", 404)
    try:
        pick = state.make_pick(player, body.get("team_id"), body.get("price"))
    except ValueError as exc:
        conn.close()
        raise ApiError(str(exc)) from exc
    store.save_picks(conn, cfg["id"], state.slots)
    conn.close()
    return {"pick": pick.to_dict()}


@route("POST", "/api/league/<league_id>/draft/undo")
def undo_pick(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    conn, state = _draft_state(api, cfg)
    undone = state.undo()
    store.save_picks(conn, cfg["id"], state.slots)
    conn.close()
    return {"undone": undone.to_dict() if undone else None}


@route("POST", "/api/league/<league_id>/draft/reset")
def reset_draft(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    conn = api.conn()
    store.clear_draft(conn, cfg["id"])
    conn.close()
    return {"ok": True}


@route("GET", "/api/league/<league_id>/draft/recommend")
def draft_recommend(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    conn, state = _draft_state(api, cfg)
    pool = build_pool(cfg)
    conn.close()

    clock = state.on_the_clock
    team_id = api.arg("team") or (clock.team_id if clock else cfg.get("my_team_id"))
    if not team_id:
        raise ApiError("No team to recommend for.")

    by_id = index_by_id(pool)
    roster = [by_id[pid] for pid in state.roster_ids(team_id) if pid in by_id]
    return {
        "team_id": team_id,
        "picks_until_next": state.picks_until_next(team_id),
        "recommendations": recommend(state, pool, team_id, int(api.arg("limit") or 12)),
        "roster": [p.to_dict() for p in roster],
        "lineup": describe_lineup(roster, cfg),
    }


@route("POST", "/api/league/<league_id>/draft/finish")
def finish_draft(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    """Freeze the draft into season rosters."""
    cfg = api.load_cfg(params["league_id"])
    conn = api.conn()
    count = store.seed_rosters_from_draft(conn, cfg["id"])
    conn.close()
    return {"ok": True, "players": count}


# --------------------------------------------------------------------------
# Rosters, lineups, trades
# --------------------------------------------------------------------------

def _rosters(api: Api, cfg: dict[str, Any]) -> dict[str, list[Any]]:
    """Season rosters, falling back to the draft while it is in progress."""
    conn = api.conn()
    stored = store.load_rosters(conn, cfg["id"])
    if not stored:
        picks = store.load_picks(conn, cfg["id"])
        stored = {}
        for pick in picks:
            if pick.player_id:
                stored.setdefault(pick.team_id, []).append(pick.player_id)
    conn.close()

    by_id = index_by_id(build_pool(cfg))
    return {
        team_id: [by_id[pid] for pid in ids if pid in by_id]
        for team_id, ids in stored.items()
    }


@route("GET", "/api/league/<league_id>/rosters")
def get_rosters(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    rosters = _rosters(api, cfg)
    return {
        "rosters": {
            team_id: {
                "players": [p.to_dict() for p in players],
                "lineup_value": optimal_lineup(players, cfg)[1],
            }
            for team_id, players in rosters.items()
        },
        "teams": cfg.get("teams", []),
    }


@route("POST", "/api/league/<league_id>/rosters/<team_id>")
def set_team_roster(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    conn = api.conn()
    store.set_roster(conn, cfg["id"], params["team_id"], body.get("player_ids") or [])
    conn.close()
    return {"ok": True}


@route("GET", "/api/league/<league_id>/lineup")
def get_lineup(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    team_id = api.arg("team") or cfg.get("my_team_id")
    if not team_id:
        raise ApiError("Pick a team first.")
    players = _rosters(api, cfg).get(team_id, [])
    lineup, value = optimal_lineup(players, cfg)
    starting = {id(p) for group in lineup.values() for p in group}
    return {
        "team_id": team_id,
        "lineup": describe_lineup(players, cfg),
        "bench": [p.to_dict() for p in players if id(p) not in starting],
        "projected": value,
    }


@route("POST", "/api/league/<league_id>/trades/evaluate")
def evaluate_trade(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    rosters = _rosters(api, cfg)
    a_id = body.get("team_a") or cfg.get("my_team_id")
    b_id = body.get("team_b")
    if not a_id or not b_id:
        raise ApiError("Both sides of the trade need a team.")

    a_roster = rosters.get(a_id, [])
    b_roster = rosters.get(b_id, [])
    a_index = {p.player_id: p for p in a_roster}
    b_index = {p.player_id: p for p in b_roster}

    a_sends = [a_index[pid] for pid in body.get("a_sends") or [] if pid in a_index]
    b_sends = [b_index[pid] for pid in body.get("b_sends") or [] if pid in b_index]
    if not a_sends and not b_sends:
        raise ApiError("Select at least one player to trade.")

    week = body.get("week")
    return trades.evaluate(
        cfg, a_roster, b_roster, a_sends, b_sends, int(week) if week else None
    )


@route("GET", "/api/league/<league_id>/trades/suggest")
def suggest_trades(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    rosters = _rosters(api, cfg)
    team_id = api.arg("team") or cfg.get("my_team_id")
    if not team_id:
        raise ApiError("Pick your team first.")
    if team_id not in rosters:
        raise ApiError("That team has no roster yet — finish the draft first.")

    week = api.arg("week")
    others = {tid: players for tid, players in rosters.items() if tid != team_id}
    suggestions = trades.suggest(
        cfg,
        rosters[team_id],
        others,
        week=int(week) if week else None,
        limit=int(api.arg("limit") or 10),
    )
    return {"team_id": team_id, "suggestions": suggestions, "teams": cfg.get("teams", [])}


@route("POST", "/api/league/<league_id>/trades/save")
def save_trade(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    conn = api.conn()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    trade_id = store.save_trade(conn, cfg["id"], body.get("trade") or {}, now)
    conn.close()
    return {"id": trade_id}


@route("GET", "/api/league/<league_id>/trades/saved")
def saved_trades(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    conn = api.conn()
    rows = store.list_trades(conn, cfg["id"])
    conn.close()
    return {"trades": rows}


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "FFAssistant"

    def do_GET(self) -> None:          # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._dispatch("GET", parsed)
        else:
            self._serve_static(parsed.path)

    def do_POST(self) -> None:         # noqa: N802
        self._dispatch("POST", urlparse(self.path))

    def do_PUT(self) -> None:          # noqa: N802
        self._dispatch("PUT", urlparse(self.path))

    def do_DELETE(self) -> None:       # noqa: N802
        self._dispatch("DELETE", urlparse(self.path))

    # -- helpers -----------------------------------------------------------

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError("Request body was not valid JSON.") from exc
        return parsed if isinstance(parsed, dict) else {}

    def _dispatch(self, method: str, parsed: Any) -> None:
        try:
            body = self._read_body() if method != "GET" else {}
            for route_method, regex, func in _ROUTES:
                if route_method != method:
                    continue
                match = regex.match(parsed.path)
                if not match:
                    continue
                api = Api(parse_qs(parsed.query), body)
                result = func(api, match.groupdict(), body)
                self._json(200, result)
                return
            self._json(404, {"error": f"No route for {method} {parsed.path}"})
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception as exc:                       # noqa: BLE001
            traceback.print_exc()
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            target = STATIC_DIR / "index.html"
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".json": "application/json",
            ".svg": "image/svg+xml",
        }.get(target.suffix, "application/octet-stream")

        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)


def serve(host: str = "127.0.0.1", port: int = 8777) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"\n  Fantasy assistant running at  http://{host}:{port}\n")
    print("  Press Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
