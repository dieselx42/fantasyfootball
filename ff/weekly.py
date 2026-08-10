"""One week's numbers, derived from whatever projections you actually have.

Season-long projections answer the draft question. They answer almost nothing
in-season, where every decision is "who do I start *this Sunday*" — and this
Sunday has byes, injuries and a specific opponent in it.

This module is the bridge. It takes the season pool and produces a pool
projected for a single week, using, in order of preference:

  1. a projection row imported for that week (a CSV with a ``week`` column),
  2. otherwise the season total divided by the games in a season.

Then it applies availability: a player on bye scores nothing, and an injury
status scales the projection down (see :data:`ff.players.STATUS_MULTIPLIERS`).
Because that lands in ``points``, the existing lineup optimiser benches an
injured starter on its own — there is no start/sit special case anywhere.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Mapping, Sequence

from .players import (
    Player,
    load_projections,
    normalize_status,
    score_pool,
    status_multiplier,
)
from .pool import build_pool
from .valuation import compute_values

#: Games an NFL team plays in a season. A season-long projection is already
#: spread across these, so dividing by it gives a per-game number and the bye
#: is handled separately.
GAMES_PER_SEASON = 17


def games_per_season(cfg: Mapping[str, Any]) -> int:
    misc = cfg.get("settings_misc") or {}
    try:
        return max(1, int(misc.get("games_per_season") or GAMES_PER_SEASON))
    except (TypeError, ValueError):
        return GAMES_PER_SEASON


def per_game(points: float, cfg: Mapping[str, Any]) -> float:
    return float(points) / games_per_season(cfg)


def regular_season_weeks(cfg: Mapping[str, Any]) -> list[int]:
    """Weeks that count towards the standings, from the config's own settings."""
    misc = cfg.get("settings_misc") or {}
    start = int(misc.get("start_scoring_week") or 1)
    playoff_weeks = [int(w) for w in (cfg.get("playoffs") or {}).get("weeks") or []]
    end = (min(playoff_weeks) - 1) if playoff_weeks else 14
    return list(range(start, max(start, end) + 1))


def season_weeks(cfg: Mapping[str, Any]) -> list[int]:
    """Every week the league plays, regular season plus playoffs."""
    playoff_weeks = sorted(
        int(w) for w in (cfg.get("playoffs") or {}).get("weeks") or []
    )
    return regular_season_weeks(cfg) + playoff_weeks


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

def availability(player: Player, week: int | None) -> str:
    """The status code that applies to this player in this week.

    A bye outranks everything else: a healthy player on bye is exactly as
    startable as an inactive one.
    """
    if week is not None and player.bye and int(player.bye) == int(week):
        return "BYE"
    return normalize_status(player.status)


AVAILABILITY_NOTES = {
    "BYE": "on bye this week",
    "O": "ruled out",
    "IR": "on injured reserve",
    "SUS": "suspended",
    "NA": "not active",
    "D": "doubtful — treat as out unless he practices Friday",
    "Q": "questionable — check the Sunday inactives",
}


def availability_note(player: Player, week: int | None) -> str:
    return AVAILABILITY_NOTES.get(availability(player, week), "")


# --------------------------------------------------------------------------
# The weekly pool
# --------------------------------------------------------------------------

def week_pool(
    cfg: Mapping[str, Any],
    week: int,
    statuses: Mapping[str, str] | None = None,
    season_pool: Sequence[Player] | None = None,
) -> list[Player]:
    """Every player, projected and valued for ``week``.

    ``statuses`` is a map of ``player_id`` -> status code that overrides
    whatever the projection file said. That is the Sunday-morning path: a beat
    writer reports someone inactive at 11:28, you mark him out, and the lineup
    re-solves without re-importing anything.

    Returns fresh :class:`~ff.players.Player` objects — the cached season pool
    is never mutated.
    """
    season = list(season_pool if season_pool is not None else build_pool(cfg))
    weekly_rows = weekly_rows_for(cfg, week)
    overrides = {
        pid: normalize_status(code) for pid, code in (statuses or {}).items()
    }

    out: list[Player] = []
    for base in season:
        row = weekly_rows.get(base.player_id)
        has_weekly = row is not None and (row.stats or row.points)

        player = replace(
            base,
            week=week,
            points=row.points if has_weekly else per_game(base.points, cfg),
            stats=dict(row.stats) if has_weekly else {},
            opponent=(row.opponent if row is not None else "") or "",
            source=(row.source if has_weekly else base.source),
            season_points=base.points,
            vor=0.0,
            pos_rank=0,
            value_tier=0,
        )

        reported = row.status if (row is not None and row.status) else base.status
        player.status = overrides.get(base.player_id, reported)
        player.status = availability(player, week)
        # Negative projections are real (a defence can lose you points), so
        # this scales rather than clamps.
        player.points *= status_multiplier(player.status, cfg)
        out.append(player)

    return compute_values(out, cfg)


def weekly_rows_for(cfg: Mapping[str, Any], week: int) -> dict[str, Player]:
    """Imported projection rows carrying ``week``, scored with league rules."""
    rows = score_pool(load_projections(week=week), cfg)
    return {p.player_id: p for p in rows}


def index_by_id(players: Iterable[Player]) -> dict[str, Player]:
    return {p.player_id: p for p in players}


def has_weekly_projections(cfg: Mapping[str, Any], week: int) -> bool:
    """Whether this week's numbers are real or spread from a season total.

    Worth surfacing: a per-game average says nothing about a matchup, and
    acting on it as though it did is the most common way a good tool gives bad
    advice.
    """
    return bool(weekly_rows_for(cfg, week))
