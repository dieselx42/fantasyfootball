"""Roster shape, slot filling, and lineup optimisation.

Used by three callers that all need the same question answered — "what is the
best legal starting lineup from this set of players, and what is it worth?":

  * the draft board, to score roster need
  * weekly start/sit
  * the trade engine, to decide whether a deal actually improves a team
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .players import Player


def slot_definitions(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        slot
        for slot in (cfg.get("roster") or {}).get("slots", [])
        if int(slot.get("count") or 0) > 0
    ]


def optimal_lineup(
    players: Sequence[Player],
    cfg: Mapping[str, Any],
    key: str = "points",
) -> tuple[dict[str, list[Player]], float]:
    """Best legal starting lineup and its total value.

    Slots are filled most-restrictive-first (a QB-only slot before a
    RB/WR/TE FLEX), which yields the optimal assignment whenever slot
    eligibility is nested — the case in every mainstream fantasy format.
    """
    remaining = sorted(players, key=lambda p: getattr(p, key, 0.0), reverse=True)
    lineup: dict[str, list[Player]] = {}
    total = 0.0

    for slot in sorted(slot_definitions(cfg), key=lambda s: len(s.get("eligible") or [])):
        name = slot["slot"]
        eligible = set(slot.get("eligible") or [])
        picked: list[Player] = []
        for _ in range(int(slot["count"])):
            choice = next((p for p in remaining if p.pos in eligible), None)
            if choice is None:
                break
            remaining.remove(choice)
            picked.append(choice)
            total += float(getattr(choice, key, 0.0))
        lineup[name] = picked

    return lineup, round(total, 2)


def lineup_value(
    players: Sequence[Player], cfg: Mapping[str, Any], key: str = "points"
) -> float:
    return optimal_lineup(players, cfg, key)[1]


def bench(players: Sequence[Player], cfg: Mapping[str, Any]) -> list[Player]:
    lineup, _ = optimal_lineup(players, cfg)
    starting = {id(p) for group in lineup.values() for p in group}
    return [p for p in players if id(p) not in starting]


def position_counts(players: Iterable[Player]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for player in players:
        counts[player.pos] = counts.get(player.pos, 0) + 1
    return counts


def unfilled_slots(
    players: Sequence[Player], cfg: Mapping[str, Any]
) -> dict[str, int]:
    """Starting slots this roster cannot currently fill."""
    lineup, _ = optimal_lineup(players, cfg)
    gaps = {}
    for slot in slot_definitions(cfg):
        missing = int(slot["count"]) - len(lineup.get(slot["slot"], []))
        if missing > 0:
            gaps[slot["slot"]] = missing
    return gaps


def need_multiplier(
    roster: Sequence[Player], pos: str, cfg: Mapping[str, Any]
) -> float:
    """How much a team should prefer ``pos`` given who it already has.

    Above 1.0 while starting slots are unfilled, decaying below 1.0 once the
    position is stocked. ``valuation.need_weight`` controls how opinionated
    this is — 0 turns it off entirely for a pure best-player-available board.
    """
    weight = float((cfg.get("valuation") or {}).get("need_weight", 0.25))
    if weight <= 0:
        return 1.0

    have = position_counts(roster).get(pos, 0)
    want = 0.0
    for slot in slot_definitions(cfg):
        eligible = slot.get("eligible") or []
        if pos in eligible:
            want += int(slot["count"]) / len(eligible)

    if want <= 0:
        return 1.0
    shortfall = (want - have) / want          # 1.0 when empty, negative when stocked
    return max(0.1, 1.0 + weight * shortfall)


def describe_lineup(
    players: Sequence[Player], cfg: Mapping[str, Any]
) -> list[dict[str, Any]]:
    lineup, _ = optimal_lineup(players, cfg)
    rows = []
    for slot in slot_definitions(cfg):
        name = slot["slot"]
        picked = lineup.get(name, [])
        for i in range(int(slot["count"])):
            player = picked[i] if i < len(picked) else None
            rows.append(
                {
                    "slot": name,
                    "player": player.to_dict() if player else None,
                }
            )
    return rows
