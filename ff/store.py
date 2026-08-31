"""Season state: draft picks, rosters, saved trades.

A thin layer over the active storage backend. It exists so callers work in
``Pick`` objects rather than raw rows, and so the web layer never imports a
database driver.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .draft import Pick
from .storage import get_backend
from .transactions import Transaction, apply_to_rosters


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


# --- weekly scores -------------------------------------------------------

def load_scores(league_id: str) -> dict[int, dict[str, float]]:
    return get_backend().load_scores(league_id)


def set_week_scores(league_id: str, week: int, scores: Mapping[str, float]) -> None:
    get_backend().set_week_scores(league_id, int(week), dict(scores))


# --- transactions --------------------------------------------------------

def list_transactions(league_id: str) -> list[Transaction]:
    return [
        Transaction.from_dict(row) for row in get_backend().list_transactions(league_id)
    ]


def add_transaction(league_id: str, txn: Transaction) -> int:
    return get_backend().add_transaction(league_id, txn.to_dict())


def delete_transaction(league_id: str, txn_id: int) -> bool:
    return get_backend().delete_transaction(league_id, int(txn_id))


# --- weekly player status ------------------------------------------------

def load_statuses(league_id: str, week: int) -> dict[str, str]:
    return get_backend().load_statuses(league_id, int(week))


def set_status(league_id: str, week: int, player_id: str, status: str) -> None:
    get_backend().set_status(league_id, int(week), player_id, status)


# --- watchlist -----------------------------------------------------------

def list_watchlist(league_id: str) -> list[dict[str, Any]]:
    return get_backend().list_watchlist(league_id)


def watch_player(
    league_id: str, player_id: str, note: str = "", added: str | None = None
) -> None:
    stamp = added or datetime.now(timezone.utc).isoformat(timespec="seconds")
    get_backend().watch_player(league_id, player_id, note or "", stamp)


def unwatch_player(league_id: str, player_id: str) -> bool:
    return get_backend().unwatch_player(league_id, player_id)


# --- current rosters -----------------------------------------------------

def current_roster_ids(league_id: str) -> dict[str, list[str]]:
    """Who owns whom right now.

    Stored rosters are the post-draft snapshot; the transaction log is
    replayed over them so an add made three weeks ago is reflected without
    anyone having re-entered a roster. Falls back to the draft board while the
    draft is still in progress.
    """
    base = load_rosters(league_id)
    if not base:
        base = {}
        for pick in load_picks(league_id):
            if pick.player_id:
                base.setdefault(pick.team_id, []).append(pick.player_id)
    return apply_to_rosters(base, list_transactions(league_id))
