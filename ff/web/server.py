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
from .. import matchups, store, sync, trades, waivers
from ..draft import DraftState, board_summary, draft_plan, recommend
from ..platforms import (
    PlatformError,
    PlatformUnsupported,
    adapter_for,
    describe_all,
    get_adapter,
)
from ..players import Player, normalize_status, parse_projection_csv
from ..storage import get_backend
from ..pool import build_pool, index_by_id, invalidate
from ..roster import describe_lineup, optimal_lineup
from ..scoring_vocab import STAT_GROUPS
from ..transactions import Transaction, acquisitions, activity
from ..transactions import validate as validate_transaction
from ..valuation import depth_warnings, replacement_levels
from ..weekly import availability_note, has_weekly_projections, season_weeks, week_pool

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
        "projection_files": [f["name"] for f in get_backend().list_projection_sets()],
        "storage": get_backend().describe(),
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
    cfg = api.load_cfg(params["league_id"])
    return {"league": cfg, "schedule": cfgmod.draft_schedule(cfg)}


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
        adapter = adapter_for(cfg)
        status = adapter.status()
        status["capabilities"] = adapter.describe_capabilities()
        status["label"] = adapter.label
        return status
    except PlatformError as exc:
        return {"ready": False, "detail": str(exc), "capabilities": []}


# --------------------------------------------------------------------------
# Syncing from the platform
#
# One route per capability. The UI renders a button for each capability the
# connected adapter declares, so a new platform integration needs no UI work
# and a partial one offers exactly what it can deliver.
# --------------------------------------------------------------------------

def _sync(api: Api, cfg: dict[str, Any], operation: str) -> Any:
    """Run one sync operation, turning platform failures into clean errors."""
    try:
        if operation == "league":
            result = sync.sync_league(cfg)
            cfgmod.save(cfg)
            invalidate(cfg["id"])
            return result

        if operation == "rules":
            return sync.sync_rules(cfg)

        if operation == "rosters":
            return sync.sync_rosters(cfg, build_pool(cfg))

        if operation == "projections":
            # No week means season-long totals, which is what the draft board
            # wants; a week means matchup-aware numbers for the Week tab.
            raw = api.arg("week")
            result = sync.sync_projections(cfg, int(raw) if raw else None)
            invalidate()
            return result

        if operation == "transactions":
            return sync.sync_transactions(cfg, build_pool(cfg))

        if operation == "draft":
            return sync.sync_draft(cfg, build_pool(cfg))

        if operation == "scoreboard":
            week = _week_arg(api, cfg, {})
            result = sync.sync_scoreboard(cfg, week)
            # The platform's real fixtures beat our generated round robin.
            if sync.merge_schedule(cfg, week, result.get("games") or []):
                cfgmod.save(cfg)
            return result
    except PlatformUnsupported as exc:
        raise ApiError(str(exc), 501) from exc
    except PlatformError as exc:
        raise ApiError(str(exc), 502) from exc
    except cfgmod.ConfigError as exc:
        # Imported data that will not validate. Nothing was written, because
        # save() validates before it persists — say so, rather than letting a
        # bare 500 imply the league is now half-updated.
        raise ApiError(
            f"The imported data did not validate, so nothing was changed: {exc}"
        ) from exc

    raise ApiError(f"Nothing syncs '{operation}'.", 404)


@route("POST", "/api/league/<league_id>/sync/<operation>")
def run_sync(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    return _sync(api, cfg, params["operation"])


@route("POST", "/api/league/<league_id>/sync/rules/apply")
def apply_synced_rules(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    """Write imported scoring and roster rules into the league config.

    Separate from reading them on purpose: scoring drives every valuation in
    the app, so overwriting it is a decision worth confirming rather than a
    side effect of pressing sync.
    """
    cfg = api.load_cfg(params["league_id"])
    proposed = body.get("proposed")
    if not isinstance(proposed, dict) or not proposed:
        raise ApiError("Nothing to apply.")

    written = sync.apply_rules(cfg, proposed)
    problems = cfgmod.validate(cfg)
    if problems:
        raise ApiError(
            "The imported rules did not validate, so nothing was changed: "
            + "; ".join(problems)
        )
    cfgmod.save(cfg)
    invalidate(cfg["id"])
    return {"ok": True, "applied": written, "league": cfg}


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

    get_backend().save_projection_set(filename, text)
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
    return {"files": get_backend().list_projection_sets()}


@route("DELETE", "/api/projections/<name>")
def delete_projections(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    if not get_backend().delete_projection_set(Path(params["name"]).name):
        raise ApiError("No such projection file.", 404)
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

def _draft_state(cfg: dict[str, Any]) -> DraftState:
    return DraftState(cfg, store.load_picks(cfg["id"]))


@route("GET", "/api/league/<league_id>/draft")
def get_draft(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    if not cfg.get("teams"):
        raise ApiError("Add your teams before starting a draft.")
    state = _draft_state(cfg)
    pool = build_pool(cfg)
    return {
        "board": board_summary(state, pool),
        "picks": [p.to_dict() for p in state.slots],
        "teams": cfg.get("teams", []),
        "my_team_id": cfg.get("my_team_id"),
        "schedule": cfgmod.draft_schedule(cfg),
    }


@route("POST", "/api/league/<league_id>/draft/pick")
def make_pick(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    state = _draft_state(cfg)
    pool = build_pool(cfg)
    player = index_by_id(pool).get(body.get("player_id", ""))
    if player is None:
        raise ApiError("That player is not in the pool.", 404)
    try:
        pick = state.make_pick(player, body.get("team_id"), body.get("price"))
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    store.save_picks(cfg["id"], state.slots)
    return {"pick": pick.to_dict()}


@route("POST", "/api/league/<league_id>/draft/undo")
def undo_pick(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    state = _draft_state(cfg)
    undone = state.undo()
    store.save_picks(cfg["id"], state.slots)
    return {"undone": undone.to_dict() if undone else None}


@route("POST", "/api/league/<league_id>/draft/reset")
def reset_draft(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    store.clear_draft(cfg["id"])
    return {"ok": True}


@route("GET", "/api/league/<league_id>/draft/recommend")
def draft_recommend(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    state = _draft_state(cfg)
    pool = build_pool(cfg)

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


@route("GET", "/api/league/<league_id>/draft/plan")
def draft_plan_route(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    """Round-by-round: what the board is likely to look like at each of your
    picks. The live board answers "who now"; this answers "what does round 4
    look like from pick 6", which is the question you have the week before."""
    cfg = api.load_cfg(params["league_id"])
    team_id = api.arg("team") or cfg.get("my_team_id")
    if not team_id:
        raise ApiError("Pick a team first.")
    if not (cfg.get("draft") or {}).get("order"):
        raise ApiError(
            "Set your draft order first — without it there is no way to know "
            "where you pick, and the whole plan depends on that."
        )

    state = _draft_state(cfg)
    pool = build_pool(cfg)
    plan = draft_plan(
        cfg, pool, team_id,
        per_pick=int(api.arg("limit") or 5),
        taken=state.drafted_ids(),
    )
    return {
        "team_id": team_id,
        "slot": (cfg["draft"]["order"].index(team_id) + 1) if team_id in cfg["draft"]["order"] else None,
        "teams": cfg.get("team_count"),
        "plan": plan,
        "with_adp": sum(1 for p in pool if p.adp),
        "pool": len(pool),
    }


@route("POST", "/api/league/<league_id>/draft/finish")
def finish_draft(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    """Freeze the draft into season rosters."""
    cfg = api.load_cfg(params["league_id"])
    count = store.seed_rosters_from_draft(cfg["id"])
    return {"ok": True, "players": count}


# --------------------------------------------------------------------------
# Rosters, lineups, trades
# --------------------------------------------------------------------------

def _rosters(api: Api, cfg: dict[str, Any]) -> dict[str, list[Any]]:
    """Season rosters as they stand now.

    Post-draft rosters with the transaction log replayed over them, falling
    back to the draft board while the draft is still in progress.
    """
    return _rosters_from(cfg, build_pool(cfg))


def _rosters_from(
    cfg: dict[str, Any], pool: list[Player]
) -> dict[str, list[Player]]:
    """Attach a pool's player objects to the current roster ids.

    Taking the pool as an argument is what lets the same rosters be viewed
    through season-long numbers or a single week's.
    """
    by_id = index_by_id(pool)
    return {
        team_id: [by_id[pid] for pid in ids if pid in by_id]
        for team_id, ids in store.current_roster_ids(cfg["id"]).items()
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
    store.set_roster(cfg["id"], params["team_id"], body.get("player_ids") or [])
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
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    trade_id = store.save_trade(cfg["id"], body.get("trade") or {}, now)
    return {"id": trade_id}


@route("GET", "/api/league/<league_id>/trades/saved")
def saved_trades(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    return {"trades": store.list_trades(cfg["id"])}


# --------------------------------------------------------------------------
# The in-season week
#
# Everything below is the loop the app runs from September to December: set a
# lineup with Sunday-morning information, work the waiver wire, record what
# happened, and watch what the other nine managers are doing about it.
# --------------------------------------------------------------------------

def _week_arg(api: Api, cfg: dict[str, Any], params: dict[str, str]) -> int:
    """The week under discussion, defaulting to the next unscored one."""
    raw = params.get("week") or api.arg("week")
    if raw:
        try:
            return int(raw)
        except ValueError as exc:
            raise ApiError(f"'{raw}' is not a week number.") from exc
    return matchups.default_week(cfg, store.load_scores(cfg["id"]))


def _weekly_view(cfg: dict[str, Any], week: int) -> tuple[list[Player], dict[str, list[Player]]]:
    statuses = store.load_statuses(cfg["id"], week)
    pool = week_pool(cfg, week, statuses)
    return pool, _rosters_from(cfg, pool)


@route("GET", "/api/league/<league_id>/week")
def get_week(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    """One team's week: lineup, bench, who is unavailable, and the matchup."""
    cfg = api.load_cfg(params["league_id"])
    week = _week_arg(api, cfg, params)
    team_id = api.arg("team") or cfg.get("my_team_id")
    if not team_id:
        raise ApiError("Pick a team first.")

    pool, rosters = _weekly_view(cfg, week)
    players = rosters.get(team_id, [])
    lineup, projected = optimal_lineup(players, cfg)
    starting = {id(p) for group in lineup.values() for p in group}

    projections = matchups.project_week(cfg, week, rosters)
    summary = matchups.week_summary(
        cfg, week, store.load_scores(cfg["id"]), projections, team_id
    )

    return {
        "week": week,
        "weeks": season_weeks(cfg),
        "team_id": team_id,
        "lineup": describe_lineup(players, cfg),
        "bench": [p.to_dict() for p in players if id(p) not in starting],
        "projected": projected,
        "unavailable": [
            {**p.to_dict(), "note": availability_note(p, week)}
            for p in players
            if not p.playable
        ],
        "matchup": summary["my_game"],
        "projections": projections,
        "weekly_projections": has_weekly_projections(cfg, week),
        "statuses": store.load_statuses(cfg["id"], week),
    }


@route("POST", "/api/league/<league_id>/week/status")
def set_player_status(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    """Mark a player out (or clear it) for one week.

    This is the Sunday-11:28am path — beat writer says inactive, you mark it,
    the lineup re-solves. Stored per week so it never leaks into next week.
    """
    cfg = api.load_cfg(params["league_id"])
    week = _week_arg(api, cfg, params)
    player_id = (body.get("player_id") or "").strip()
    if not player_id:
        raise ApiError("Which player?")
    status = normalize_status(str(body.get("status") or ""))
    store.set_status(cfg["id"], week, player_id, status)
    return {"ok": True, "week": week, "player_id": player_id, "status": status}


@route("GET", "/api/league/<league_id>/waivers")
def get_waivers(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    week = _week_arg(api, cfg, params)
    team_id = api.arg("team") or cfg.get("my_team_id")
    if not team_id:
        raise ApiError("Pick a team first.")

    pool, rosters = _weekly_view(cfg, week)
    if team_id not in rosters:
        raise ApiError("That team has no roster yet — finish the draft first.")

    available = waivers.free_agents(pool, rosters)
    txns = store.list_transactions(cfg["id"])
    spent = sum(float(t.faab or 0) for t in txns if t.team_id == team_id)
    budget = float((cfg.get("waivers") or {}).get("faab_budget") or 0)

    result = waivers.recommend(
        cfg,
        rosters[team_id],
        available,
        week=week,
        limit=int(api.arg("limit") or 15),
        faab_remaining=max(0.0, budget - spent),
    )
    result["team_id"] = team_id
    result["weeks"] = season_weeks(cfg)
    result["drops"] = waivers.drop_candidates(cfg, rosters[team_id])
    result["limits"] = waivers.acquisition_limits(
        cfg,
        added_this_week=acquisitions(txns, team_id, week),
        added_this_season=acquisitions(txns, team_id),
    )
    return result


@route("GET", "/api/league/<league_id>/scoreboard")
def get_scoreboard(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    """Every game this week, projected before kickoff and final after it."""
    cfg = api.load_cfg(params["league_id"])
    week = _week_arg(api, cfg, params)
    _pool, rosters = _weekly_view(cfg, week)
    scores = store.load_scores(cfg["id"])
    summary = matchups.week_summary(
        cfg, week, scores, matchups.project_week(cfg, week, rosters)
    )
    summary["weeks"] = season_weeks(cfg)
    summary["teams"] = cfg.get("teams", [])
    summary["my_team_id"] = cfg.get("my_team_id")
    summary["recorded"] = scores.get(week, {})
    return summary


@route("POST", "/api/league/<league_id>/scores")
def record_scores(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    """Type in what each team actually scored. Standings follow from it."""
    cfg = api.load_cfg(params["league_id"])
    week = _week_arg(api, cfg, params)
    raw = body.get("scores")
    if not isinstance(raw, dict):
        raise ApiError("Send scores as {team_id: points}.")

    known = {t["id"] for t in cfg.get("teams") or []}
    scores: dict[str, float] = {}
    for team_id, points in raw.items():
        if team_id not in known:
            raise ApiError(f"'{team_id}' is not a team in this league.")
        if points in (None, ""):
            continue
        try:
            scores[team_id] = float(points)
        except (TypeError, ValueError) as exc:
            raise ApiError(f"'{points}' is not a score.") from exc

    store.set_week_scores(cfg["id"], week, scores)
    return {"ok": True, "week": week, "teams_scored": len(scores)}


@route("GET", "/api/league/<league_id>/standings")
def get_standings(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    summary = matchups.season_summary(cfg, store.load_scores(cfg["id"]))
    summary["teams"] = cfg.get("teams", [])
    return summary


@route("GET", "/api/league/<league_id>/schedule")
def get_schedule(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    return {
        "schedule": matchups.schedule(cfg),
        "teams": cfg.get("teams", []),
        "generated": not (cfg.get("schedule") or []),
        "my_team_id": cfg.get("my_team_id"),
    }


@route("GET", "/api/league/<league_id>/transactions")
def get_transactions(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    """The league's activity feed, and what it says about each rival."""
    cfg = api.load_cfg(params["league_id"])
    since = api.arg("since_week")
    pool = index_by_id(build_pool(cfg))
    return activity(
        cfg,
        store.list_transactions(cfg["id"]),
        pool,
        since_week=int(since) if since else None,
    )


@route("POST", "/api/league/<league_id>/transactions")
def add_transaction(api: Api, params: dict[str, str], body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    txn = Transaction.from_dict(body)
    if txn.week is None:
        txn.week = _week_arg(api, cfg, params)
    if not txn.date:
        txn.date = datetime.now(timezone.utc).isoformat(timespec="seconds")

    problems = validate_transaction(txn, cfg)
    if problems:
        raise ApiError("; ".join(problems))

    txn.id = store.add_transaction(cfg["id"], txn)
    return {"transaction": txn.to_dict()}


@route("DELETE", "/api/league/<league_id>/transactions/<txn_id>")
def remove_transaction(api: Api, params: dict[str, str], _body: dict[str, Any]) -> Any:
    cfg = api.load_cfg(params["league_id"])
    try:
        txn_id = int(params["txn_id"])
    except ValueError as exc:
        raise ApiError("Transaction id must be a number.") from exc
    if not store.delete_transaction(cfg["id"], txn_id):
        raise ApiError("No such transaction.", 404)
    return {"ok": True}


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

def dispatch(
    method: str,
    path: str,
    query: dict[str, list[str]],
    body: dict[str, Any],
) -> tuple[int, Any]:
    """Route one request. Transport-agnostic, so the stdlib server and the
    WSGI adapter share exactly one code path and cannot drift apart."""
    for route_method, regex, func in _ROUTES:
        if route_method != method:
            continue
        match = regex.match(path)
        if not match:
            continue
        try:
            return 200, func(Api(query, body), match.groupdict(), body)
        except ApiError as exc:
            return exc.status, {"error": str(exc)}
        except Exception as exc:                    # noqa: BLE001
            traceback.print_exc()
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
    return 404, {"error": f"No route for {method} {path}"}


def static_file(path: str) -> tuple[bytes, str] | None:
    """Read a file from the bundled frontend, or None if it escapes the tree."""
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
        ".ico": "image/x-icon",
    }.get(target.suffix, "application/octet-stream")
    return target.read_bytes(), content_type


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
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
            return
        status, payload = dispatch(method, parsed.path, parse_qs(parsed.query), body)
        self._json(status, payload)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        data, content_type = static_file(path)
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
