"""Season state: draft picks, rosters, saved trades.

A thin layer over the active storage backend. It exists so callers work in
``Pick`` objects rather than raw rows, and so the web layer never imports a
database driver.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .draft import Pick
from .storage import get_backend


def _pick_from_row(row: dict[str, Any]) -> Pick:
    return Pick(
        overall=int(row["overall"]),
        round=int(row["round"]),
        pick_in_round=int(row["pick_in_round"]),
        team_id=row["team_id"],
        player_id=row.get("player_id"),
        player_name=row.get("player_name") or "",
        pos=row.get("pos") or "",
        price=row.get("price"),
    )


# --- draft picks ---------------------------------------------------------

def save_picks(league_id: str, picks: Iterable[Pick]) -> None:
    """Persist only the picks that have actually been made."""
    get_backend().save_picks(
        league_id, [p.to_dict() for p in picks if p.player_id]
    )


def load_picks(league_id: str) -> list[Pick]:
    return [_pick_from_row(row) for row in get_backend().load_picks(league_id)]


def clear_draft(league_id: str) -> None:
    get_backend().clear_picks(league_id)


# --- rosters -------------------------------------------------------------

def set_roster(league_id: str, team_id: str, player_ids: Sequence[str]) -> None:
    get_backend().set_roster(league_id, team_id, list(player_ids))


def load_rosters(league_id: str) -> dict[str, list[str]]:
    return get_backend().load_rosters(league_id)


def seed_rosters_from_draft(league_id: str) -> int:
    """Turn a completed draft into starting rosters."""
    by_team: dict[str, list[str]] = {}
    for pick in load_picks(league_id):
        if pick.player_id:
            by_team.setdefault(pick.team_id, []).append(pick.player_id)
    for team_id, ids in by_team.items():
        set_roster(league_id, team_id, ids)
    return sum(len(v) for v in by_team.values())


# --- trades --------------------------------------------------------------

def save_trade(league_id: str, payload: dict[str, Any], created: str) -> int:
    return get_backend().save_trade(league_id, payload, created)


def list_trades(league_id: str) -> list[dict[str, Any]]:
    return get_backend().list_trades(league_id)
