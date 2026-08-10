"""Value Over Replacement, positional scarcity, and tiers.

Raw projected points cannot be compared across positions: a QB who scores 340
is not better than an RB who scores 260, because *every* team can start a
~300-point QB while a 260-point RB might be the 8th best available. Value Over
Replacement (VOR) fixes this by subtracting, for each position, what a team
could get for free at that position.

Replacement level is derived from the league's own roster settings, so a
superflex or TE-premium league automatically values positions differently
without a line of special-case code.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .config import POSITIONS, starters_by_position
from .players import Player


def replacement_levels(
    players: Sequence[Player], cfg: Mapping[str, Any]
) -> dict[str, float]:
    """Points scored by the 'freely available' player at each position.

    With 12 teams starting 2.45 RBs on average, roughly the 30th RB is the last
    one who will be started weekly — that player's projection is the baseline
    every other RB is measured against.
    """
    valuation = cfg.get("valuation") or {}
    override = valuation.get("replacement")
    team_count = int(cfg.get("team_count") or 12)
    starters = starters_by_position(cfg)

    by_pos: dict[str, list[float]] = {pos: [] for pos in POSITIONS}
    for player in players:
        if player.pos in by_pos:
            by_pos[player.pos].append(player.points)
    for values in by_pos.values():
        values.sort(reverse=True)

    levels: dict[str, float] = {}
    for pos in POSITIONS:
        values = by_pos[pos]
        if not values:
            levels[pos] = 0.0
            continue

        if isinstance(override, dict) and pos in override:
            index = int(override[pos]) - 1
        else:
            # The last startable player at this position across the league.
            index = int(round(starters.get(pos, 0.0) * team_count)) - 1

        index = max(0, min(index, len(values) - 1))
        levels[pos] = values[index]

    return levels


def depth_warnings(
    players: Sequence[Player], cfg: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Positions where the loaded pool is too shallow to find replacement level.

    If a 12-team league needs the 20th QB as its baseline but only 12 QBs were
    imported, the baseline silently clamps to the worst one available — which
    *overstates* every QB's value. That failure is invisible in the numbers, so
    it has to be reported explicitly.
    """
    team_count = int(cfg.get("team_count") or 12)
    starters = starters_by_position(cfg)
    counts: dict[str, int] = {pos: 0 for pos in POSITIONS}
    for player in players:
        if player.pos in counts:
            counts[player.pos] += 1

    warnings = []
    for pos in POSITIONS:
        needed = int(round(starters.get(pos, 0.0) * team_count))
        have = counts[pos]
        if needed and have and have < needed:
            warnings.append(
                {
                    "pos": pos,
                    "have": have,
                    "needed": needed,
                    "message": (
                        f"Only {have} {pos}s loaded, but replacement level for this "
                        f"league is the {needed}th. {pos} values are overstated — "
                        f"import a deeper projection set."
                    ),
                }
            )
    return warnings


def compute_values(
    players: Iterable[Player], cfg: Mapping[str, Any]
) -> list[Player]:
    """Annotate every player with ``vor``, ``pos_rank`` and ``value_tier``.

    Mutates and returns the players, sorted best-first by VOR.
    """
    pool = list(players)
    method = (cfg.get("valuation") or {}).get("method", "vor")
    levels = replacement_levels(pool, cfg)

    for player in pool:
        baseline = levels.get(player.pos, 0.0)
        player.vor = player.points - baseline if method == "vor" else player.points

    # Positional ranks.
    for pos in POSITIONS:
        at_pos = sorted(
            (p for p in pool if p.pos == pos), key=lambda p: p.points, reverse=True
        )
        for i, player in enumerate(at_pos, start=1):
            player.pos_rank = i
        _assign_tiers(at_pos, cfg)

    pool.sort(key=lambda p: p.vor, reverse=True)
    return pool


def _assign_tiers(at_pos: Sequence[Player], cfg: Mapping[str, Any]) -> None:
    """Group a position into tiers by finding gaps in the points curve.

    Tiers are what actually drive good draft decisions: the question is never
    "who is best?" but "will someone from this group still be here next time?"
    A new tier starts wherever the drop to the next player is unusually large.
    """
    if not at_pos:
        return
    if len(at_pos) == 1:
        at_pos[0].value_tier = 1
        return

    gaps = [
        at_pos[i].points - at_pos[i + 1].points for i in range(len(at_pos) - 1)
    ]
    positive = [g for g in gaps if g > 0]
    if not positive:
        for player in at_pos:
            player.value_tier = 1
        return

    mean = sum(positive) / len(positive)
    variance = sum((g - mean) ** 2 for g in positive) / len(positive)
    stdev = variance ** 0.5
    sensitivity = float((cfg.get("valuation") or {}).get("tier_break_sensitivity", 1.0))
    threshold = mean + stdev * sensitivity

    tier = 1
    at_pos[0].value_tier = 1
    for i, gap in enumerate(gaps):
        if gap >= threshold:
            tier += 1
        at_pos[i + 1].value_tier = tier


def scarcity_report(
    players: Sequence[Player], cfg: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """How thin each position is *right now*, given who is still available.

    ``cliff`` is the points you lose by waiting until after the current tier is
    gone — the single most useful number on a draft board.
    """
    rows = []
    levels = replacement_levels(players, cfg)

    for pos in POSITIONS:
        at_pos = sorted(
            (p for p in players if p.pos == pos), key=lambda p: p.points, reverse=True
        )
        if not at_pos:
            continue
        top_tier = [p for p in at_pos if p.value_tier == at_pos[0].value_tier]
        next_up = next((p for p in at_pos if p.value_tier != at_pos[0].value_tier), None)
        cliff = (top_tier[-1].points - next_up.points) if next_up else 0.0
        rows.append(
            {
                "pos": pos,
                "available": len(at_pos),
                "best": at_pos[0].name,
                "best_points": round(at_pos[0].points, 1),
                "replacement": round(levels.get(pos, 0.0), 1),
                "tier_size": len(top_tier),
                "cliff": round(cliff, 1),
            }
        )

    rows.sort(key=lambda r: r["cliff"], reverse=True)
    return rows
