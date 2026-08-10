"""Assembling and caching the scored, valued player pool.

Rebuilding the pool means re-reading every projection CSV and re-running
valuation. That is cheap (milliseconds for a few hundred players) but it
happens on every draft-board refresh, so results are cached and invalidated
when either the projections on disk or the league's scoring rules change.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .players import Player, available_projection_files, load_projections, score_pool
from .valuation import compute_values

_CACHE: dict[str, tuple[str, list[Player]]] = {}


def _fingerprint(cfg: Mapping[str, Any]) -> str:
    """Changes whenever anything that affects player values changes."""
    files = [
        (path.name, path.stat().st_mtime_ns, path.stat().st_size)
        for path in available_projection_files()
    ]
    relevant = {
        "files": files,
        "scoring": cfg.get("scoring"),
        "bonuses": cfg.get("scoring_bonuses"),
        "valuation": cfg.get("valuation"),
        "roster": cfg.get("roster"),
        "team_count": cfg.get("team_count"),
    }
    return json.dumps(relevant, sort_keys=True, default=str)


def build_pool(cfg: Mapping[str, Any], use_cache: bool = True) -> list[Player]:
    league_id = str(cfg.get("id") or "default")
    fingerprint = _fingerprint(cfg)

    if use_cache:
        cached = _CACHE.get(league_id)
        if cached and cached[0] == fingerprint:
            return cached[1]

    players = load_projections()
    players = score_pool(players, cfg)
    players = compute_values(players, cfg)

    _CACHE[league_id] = (fingerprint, players)
    return players


def invalidate(league_id: str | None = None) -> None:
    if league_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(league_id, None)


def index_by_id(players: list[Player]) -> dict[str, Player]:
    return {p.player_id: p for p in players}
