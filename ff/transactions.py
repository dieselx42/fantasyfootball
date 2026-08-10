"""What every team in the league is doing, and what it does to the rosters.

Your own moves are only half the information. The other half is the wire
itself: which positions the league is chasing, who just dropped a starter, who
spent a third of their FAAB on a backup running back — because that last one
tells you the handcuff you were counting on is gone, and it tells you before
the box scores do.

Transactions are stored as an append-only log rather than by mutating rosters
directly. Rosters are then *derived* by replaying the log over the draft
result, which means a mistyped add is fixed by deleting one row instead of
reconstructing a roster by hand, and the history stays intact either way.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .players import Player

#: Every kind of move a league makes. ``add_drop`` is the common waiver-wire
#: shape — one in, one out, atomically.
TYPES = ("add", "drop", "add_drop", "waiver", "trade")

#: Types that move players in both directions between two teams.
TWO_SIDED = ("trade",)


@dataclass
class Transaction:
    """One roster move. ``adds`` and ``drops`` are always from the point of
    view of ``team_id``; in a trade the partner's side is the mirror image."""

    type: str
    team_id: str
    adds: list[str] = field(default_factory=list)
    drops: list[str] = field(default_factory=list)
    partner_team_id: str | None = None
    week: int | None = None
    date: str = ""
    faab: float | None = None
    note: str = ""
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "Transaction":
        return cls(
            type=str(row.get("type") or "add"),
            team_id=str(row.get("team_id") or ""),
            adds=list(row.get("adds") or []),
            drops=list(row.get("drops") or []),
            partner_team_id=row.get("partner_team_id") or None,
            week=int(row["week"]) if row.get("week") not in (None, "") else None,
            date=str(row.get("date") or ""),
            faab=float(row["faab"]) if row.get("faab") not in (None, "") else None,
            note=str(row.get("note") or ""),
            id=int(row["id"]) if row.get("id") not in (None, "") else None,
        )


def validate(txn: Transaction, cfg: Mapping[str, Any]) -> list[str]:
    """Human-readable problems, empty when the move is well formed."""
    problems: list[str] = []
    known = {t["id"] for t in cfg.get("teams") or []}

    if txn.type not in TYPES:
        problems.append(f"Unknown transaction type '{txn.type}'.")
    if txn.team_id not in known:
        problems.append(f"'{txn.team_id}' is not a team in this league.")
    if txn.type in TWO_SIDED:
        if not txn.partner_team_id:
            problems.append("A trade needs a second team.")
        elif txn.partner_team_id not in known:
            problems.append(f"'{txn.partner_team_id}' is not a team in this league.")
        elif txn.partner_team_id == txn.team_id:
            problems.append("A team cannot trade with itself.")
    if not txn.adds and not txn.drops:
        problems.append("A transaction has to move at least one player.")

    faab_budget = float((cfg.get("waivers") or {}).get("faab_budget") or 0)
    if txn.faab is not None:
        if txn.faab < 0:
            problems.append("A FAAB bid cannot be negative.")
        elif faab_budget and txn.faab > faab_budget:
            problems.append(
                f"A FAAB bid of {txn.faab:g} exceeds the league budget of "
                f"{faab_budget:g}."
            )

    return problems


# --------------------------------------------------------------------------
# Replaying the log
# --------------------------------------------------------------------------

def _sorted(transactions: Iterable[Transaction]) -> list[Transaction]:
    """Oldest first. Ids are assigned on insert, so they are the true order;
    rows without one keep their position via a stable sort."""
    rows = list(transactions)
    return sorted(rows, key=lambda t: t.id if t.id is not None else 0)


def _move(
    rosters: dict[str, list[str]],
    team_id: str,
    adds: Sequence[str],
    drops: Sequence[str],
) -> None:
    roster = rosters.setdefault(team_id, [])
    for player_id in drops:
        if player_id in roster:
            roster.remove(player_id)
    for player_id in adds:
        if player_id not in roster:
            roster.append(player_id)


def apply_to_rosters(
    base: Mapping[str, Sequence[str]], transactions: Iterable[Transaction]
) -> dict[str, list[str]]:
    """Replay the log over post-draft rosters to get current ones."""
    rosters = {team_id: list(ids) for team_id, ids in base.items()}
    for txn in _sorted(transactions):
        if txn.type in TWO_SIDED and txn.partner_team_id:
            _move(rosters, txn.team_id, txn.adds, txn.drops)
            _move(rosters, txn.partner_team_id, txn.drops, txn.adds)
        else:
            _move(rosters, txn.team_id, txn.adds, txn.drops)
    return rosters


# --------------------------------------------------------------------------
# Reading the wire
# --------------------------------------------------------------------------

def describe(
    txn: Transaction,
    players: Mapping[str, Player],
    teams: Mapping[str, str] | None = None,
) -> str:
    """One line for the activity feed."""
    teams = teams or {}
    who = teams.get(txn.team_id, txn.team_id)
    name = lambda pid: players[pid].name if pid in players else pid  # noqa: E731
    added = ", ".join(name(p) for p in txn.adds)
    dropped = ", ".join(name(p) for p in txn.drops)

    if txn.type in TWO_SIDED:
        partner = teams.get(txn.partner_team_id or "", txn.partner_team_id or "?")
        return f"{who} traded {dropped or 'nothing'} to {partner} for {added or 'nothing'}"

    bid = f" (${txn.faab:g})" if txn.faab else ""
    if added and dropped:
        return f"{who} added {added}{bid}, dropped {dropped}"
    if added:
        return f"{who} added {added}{bid}"
    return f"{who} dropped {dropped}"


def activity(
    cfg: Mapping[str, Any],
    transactions: Iterable[Transaction],
    players: Mapping[str, Player],
    since_week: int | None = None,
) -> dict[str, Any]:
    """What each team has been doing, and what the league is chasing.

    ``net_value`` is measured in value over replacement, so a team that added
    three waiver-level bodies does not look busier than one that traded for a
    starter.
    """
    rows = [
        txn for txn in _sorted(transactions)
        if since_week is None or (txn.week or 0) >= since_week
    ]
    names = {t["id"]: t.get("name", t["id"]) for t in cfg.get("teams") or []}

    by_team: dict[str, dict[str, Any]] = {
        team_id: {
            "team_id": team_id,
            "name": name,
            "adds": 0, "drops": 0, "trades": 0,
            "faab_spent": 0.0,
            "net_value": 0.0,
            "positions_added": {},
            "last_move": "",
        }
        for team_id, name in names.items()
    }
    chased: dict[str, int] = {}

    def credit(team_id: str, adds: Sequence[str], drops: Sequence[str],
               txn: Transaction, is_trade: bool) -> None:
        entry = by_team.get(team_id)
        if entry is None:
            return
        if is_trade:
            entry["trades"] += 1
        else:
            entry["adds"] += len(adds)
            entry["drops"] += len(drops)
            entry["faab_spent"] += float(txn.faab or 0)
        for player_id in adds:
            player = players.get(player_id)
            if player is None:
                continue
            entry["net_value"] += player.vor
            entry["positions_added"][player.pos] = (
                entry["positions_added"].get(player.pos, 0) + 1
            )
            chased[player.pos] = chased.get(player.pos, 0) + 1
        for player_id in drops:
            player = players.get(player_id)
            if player is not None:
                entry["net_value"] -= player.vor
        if txn.date:
            entry["last_move"] = max(entry["last_move"], txn.date)

    for txn in rows:
        is_trade = txn.type in TWO_SIDED
        credit(txn.team_id, txn.adds, txn.drops, txn, is_trade)
        if is_trade and txn.partner_team_id:
            credit(txn.partner_team_id, txn.drops, txn.adds, txn, True)

    teams = sorted(
        by_team.values(), key=lambda r: r["net_value"], reverse=True
    )
    for row in teams:
        row["net_value"] = round(row["net_value"], 2)
        row["faab_spent"] = round(row["faab_spent"], 2)

    return {
        "teams": teams,
        "feed": [
            {**txn.to_dict(), "summary": describe(txn, players, names)}
            for txn in reversed(rows)
        ],
        "positions_chased": sorted(
            ({"pos": pos, "adds": count} for pos, count in chased.items()),
            key=lambda r: r["adds"],
            reverse=True,
        ),
        "total": len(rows),
    }


def faab_spent(
    transactions: Iterable[Transaction], team_id: str
) -> float:
    return round(
        sum(float(t.faab or 0) for t in transactions if t.team_id == team_id), 2
    )


def acquisitions(
    transactions: Iterable[Transaction], team_id: str, week: int | None = None
) -> int:
    """How many players a team has added, optionally within one week."""
    total = 0
    for txn in transactions:
        if txn.team_id != team_id or txn.type in TWO_SIDED:
            continue
        if week is not None and txn.week != week:
            continue
        total += len(txn.adds)
    return total
