"""The waiver wire: who is worth adding, and who you drop to fit them.

The temptation is to rank free agents by projected points, or by value over
replacement, and stop there. Both are wrong in-season, and wrong in the same
way: they measure the player instead of measuring *the change to your team*.
The 14th-best available WR might be genuinely good and still worth nothing to
you, because he would sit behind three better WRs every week.

So the number this module leads with is **marginal lineup gain**: what your
best legal starting lineup is worth with the player, minus what it is worth
without him. When your roster is full that comparison includes the drop, since
an add you cannot fit is not an add. Everything else — value over replacement,
positional need, FAAB guidance — is reported alongside it, never in place of
it.

Two horizons are scored, because they disagree constantly and the disagreement
is the interesting part. A backup running back whose starter just tore an ACL
is worth little this week and a great deal for the rest of the season; a
streaming defence with a good matchup is the reverse.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .config import roster_capacity
from .players import Player
from .roster import lineup_value, need_multiplier, unfilled_slots
from .weekly import games_per_season

#: How many free agents get the full add/drop search. The pool is sorted by
#: value first, so this is a depth limit on an already-ranked list, not a
#: sample — but it is reported, because a silent cap reads as "we looked at
#: everyone" when we did not.
SEARCH_WIDTH = 150

DEFAULT_WEIGHTS = {
    # Points added to *this week's* starting lineup.
    "week": 1.0,
    # Points added to a typical week for the rest of the season.
    "rest_of_season": 0.6,
    # Raw value over replacement, so a player who would not start yet but is
    # clearly the best talent available still surfaces.
    "upside": 0.15,
}


def weights(cfg: Mapping[str, Any]) -> dict[str, float]:
    merged = dict(DEFAULT_WEIGHTS)
    merged.update((cfg.get("valuation") or {}).get("waiver_weights") or {})
    return {k: float(v) for k, v in merged.items()}


# --------------------------------------------------------------------------
# Who is actually available
# --------------------------------------------------------------------------

def rostered_ids(rosters: Mapping[str, Sequence[Player]]) -> set[str]:
    return {p.player_id for players in rosters.values() for p in players}


def free_agents(
    pool: Iterable[Player], taken: Iterable[str] | Mapping[str, Sequence[Player]]
) -> list[Player]:
    """Everyone in the pool nobody in the league rosters."""
    if isinstance(taken, Mapping):
        taken_ids = rostered_ids(taken)
    else:
        taken_ids = set(taken)
    return [p for p in pool if p.player_id not in taken_ids]


# --------------------------------------------------------------------------
# Recommendations
# --------------------------------------------------------------------------

def recommend(
    cfg: Mapping[str, Any],
    roster: Sequence[Player],
    available: Sequence[Player],
    *,
    week: int | None = None,
    limit: int = 15,
    faab_remaining: float | None = None,
    search_width: int = SEARCH_WIDTH,
) -> dict[str, Any]:
    """Rank waiver adds for one team.

    ``roster`` and ``available`` must both come from the same weekly pool, so
    that ``points`` means this week and ``season_points`` means the rest of it.
    """
    weight = weights(cfg)
    games = games_per_season(cfg)
    capacity = roster_capacity(cfg)
    full = len(roster) >= capacity

    base_week = lineup_value(roster, cfg)
    base_season = lineup_value(roster, cfg, key="season_points")
    gaps = unfilled_slots(roster, cfg)

    ranked = sorted(available, key=lambda p: p.vor, reverse=True)
    considered = ranked[:search_width]

    rows: list[dict[str, Any]] = []
    for candidate in considered:
        best = _best_pairing(cfg, roster, candidate, base_week, base_season, full)
        gain_week, gain_season, drop = best

        upside = max(candidate.vor, 0.0)
        score = (
            weight["week"] * gain_week
            + weight["rest_of_season"] * (gain_season / games)
            + weight["upside"] * upside
        )
        score *= need_multiplier(roster, candidate.pos, cfg)

        rows.append(
            {
                "player": candidate.to_dict(),
                "score": round(score, 2),
                "gain_week": round(gain_week, 2),
                "gain_rest_of_season": round(gain_season, 2),
                "gain_per_week": round(gain_season / games, 2),
                "vor": round(candidate.vor, 2),
                "drop": drop.to_dict() if drop else None,
                "fills_gap": candidate.pos in _gap_positions(gaps, cfg),
                "reason": "",
            }
        )

    rows.sort(key=lambda r: r["score"], reverse=True)
    top = rows[:limit]

    best_score = top[0]["score"] if top else 0.0
    for row in top:
        row["bid"] = faab_bid(cfg, row["gain_week"], base_week, faab_remaining)
        row["reason"] = _reason(row, cfg)

    return {
        "week": week,
        "recommendations": top,
        "roster_full": full,
        "roster_size": len(roster),
        "roster_capacity": capacity,
        "lineup_now": round(base_week, 2),
        "unfilled_slots": gaps,
        "considered": len(considered),
        "available": len(available),
        "truncated": len(available) - len(considered),
        "faab": _faab_state(cfg, faab_remaining),
        "best_score": round(best_score, 2),
    }


def _best_pairing(
    cfg: Mapping[str, Any],
    roster: Sequence[Player],
    candidate: Player,
    base_week: float,
    base_season: float,
    full: bool,
) -> tuple[float, float, Player | None]:
    """The add's value, paired with the cheapest drop that makes room for it.

    With a roster spot free there is nothing to weigh: adding strictly beats
    not adding. With a full roster the honest gain is add-minus-drop, and the
    right drop is whichever one costs least — which is usually not the worst
    player, but the worst player *at a position you are deep in*.
    """
    if not full:
        trial = list(roster) + [candidate]
        return (
            lineup_value(trial, cfg) - base_week,
            lineup_value(trial, cfg, key="season_points") - base_season,
            None,
        )

    best: tuple[float, float, Player | None] | None = None
    best_key = (float("-inf"), float("-inf"))
    for drop in roster:
        trial = [p for p in roster if p is not drop] + [candidate]
        gain_week = lineup_value(trial, cfg) - base_week
        gain_season = lineup_value(trial, cfg, key="season_points") - base_season
        # Rank drops on the same blend the add is ranked on, so a drop that
        # helps this week but guts the rest of the season does not win.
        combined = gain_week + gain_season / games_per_season(cfg)
        # Several drops usually tie at "changes nothing today" — every bench
        # player does. Break that tie on the dropped player's own value, or
        # the tool cheerfully suggests cutting a star who happens to be sixth
        # at a stacked position.
        key = (round(combined, 4), -drop.season_points)
        if key > best_key:
            best_key = key
            best = (gain_week, gain_season, drop)
    return best or (0.0, 0.0, None)


def _gap_positions(gaps: Mapping[str, int], cfg: Mapping[str, Any]) -> set[str]:
    """Positions that could fill a starting slot this roster cannot fill."""
    out: set[str] = set()
    for slot in (cfg.get("roster") or {}).get("slots", []):
        if gaps.get(slot.get("slot", "")):
            out.update(slot.get("eligible") or [])
    return out


def _reason(row: dict[str, Any], cfg: Mapping[str, Any]) -> str:
    """One sentence a human can agree or disagree with."""
    player = row["player"]
    name = player["name"]
    parts: list[str] = []

    if row["fills_gap"]:
        parts.append(f"fills an unfilled {player['pos']} slot")
    if row["gain_week"] >= 0.5:
        parts.append(f"starts for you this week (+{row['gain_week']:.1f} pts)")
    elif row["gain_rest_of_season"] >= 1.0:
        parts.append(
            f"does not start this week but adds "
            f"{row['gain_per_week']:.1f} pts/wk the rest of the way"
        )
    else:
        parts.append("bench depth — no lineup change today")

    if row["drop"]:
        parts.append(f"drop {row['drop']['name']}")
    if player.get("status") and player["status"] != "":
        parts.append(f"listed {player['status']}")

    return f"{name}: " + ", ".join(parts) + "."


# --------------------------------------------------------------------------
# FAAB
# --------------------------------------------------------------------------

def _faab_state(
    cfg: Mapping[str, Any], remaining: float | None
) -> dict[str, Any] | None:
    waivers = cfg.get("waivers") or {}
    if (waivers.get("type") or "") != "faab":
        return None
    budget = float(waivers.get("faab_budget") or 0)
    return {
        "budget": budget,
        "remaining": budget if remaining is None else float(remaining),
    }


def faab_bid(
    cfg: Mapping[str, Any],
    gain_week: float,
    lineup_now: float,
    remaining: float | None = None,
) -> int | None:
    """What to bid, as whole units of the league's FAAB budget.

    The anchor is how much the add improves your *starting lineup*, as a
    fraction of what that lineup already scores. A 3% lineup improvement is a
    strong waiver add and lands around 12% of your remaining budget;
    ``waivers.bid_aggressiveness`` scales that. Returns ``None`` when the
    league does not use FAAB, so callers can hide the column entirely.
    """
    state = _faab_state(cfg, remaining)
    if state is None:
        return None

    pot = state["remaining"]
    if pot <= 0 or lineup_now <= 0 or gain_week <= 0:
        return 0

    aggressiveness = float((cfg.get("waivers") or {}).get("bid_aggressiveness") or 4.0)
    share = (gain_week / lineup_now) * aggressiveness
    share = max(0.0, min(share, 0.6))          # never all-in on one player
    return max(1, round(pot * share))


# --------------------------------------------------------------------------
# Drops
# --------------------------------------------------------------------------

def drop_candidates(
    cfg: Mapping[str, Any], roster: Sequence[Player], limit: int = 5
) -> list[dict[str, Any]]:
    """Roster players ranked by how little you lose by cutting them.

    Measured the same way as adds: the drop that costs least is the one whose
    absence changes your best lineup least, this week and across the season.
    """
    if not roster:
        return []

    base_week = lineup_value(roster, cfg)
    base_season = lineup_value(roster, cfg, key="season_points")
    games = games_per_season(cfg)

    rows = []
    for player in roster:
        kept = [p for p in roster if p is not player]
        loss_week = base_week - lineup_value(kept, cfg)
        loss_season = base_season - lineup_value(kept, cfg, key="season_points")
        rows.append(
            {
                "player": player.to_dict(),
                "cost_week": round(loss_week, 2),
                "cost_rest_of_season": round(loss_season, 2),
                "cost": round(loss_week + loss_season / games, 2),
                "starter": loss_week > 0,
            }
        )

    # Cheapest first, and among the many that cost exactly nothing today, the
    # one with least left in the tank.
    rows.sort(key=lambda r: (r["cost"], r["player"]["season_points"]))
    return rows[:limit]


# --------------------------------------------------------------------------
# League rules
# --------------------------------------------------------------------------

def acquisition_limits(
    cfg: Mapping[str, Any], added_this_week: int = 0, added_this_season: int = 0
) -> list[str]:
    """Warnings when a league caps how often you can hit the wire."""
    waivers = cfg.get("waivers") or {}
    problems = []

    per_week = waivers.get("max_acquisitions_week")
    if per_week and added_this_week >= int(per_week):
        problems.append(
            f"You have used all {int(per_week)} acquisitions allowed this week."
        )

    per_season = waivers.get("max_acquisitions_season")
    if per_season and added_this_season >= int(per_season):
        problems.append(
            f"You have used all {int(per_season)} acquisitions allowed this season."
        )

    return problems
