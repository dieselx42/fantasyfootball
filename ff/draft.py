"""Live draft state and pick recommendations.

The draft board is the part of this system that has to work under time
pressure, so the model is deliberately simple: an ordered list of picks. Every
derived view (who is on the clock, what each roster looks like, who is still
available) is recomputed from that list, which makes undo trivial and makes it
impossible for the board to drift out of sync with reality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .config import draft_order, roster_capacity
from .players import Player
from .roster import need_multiplier, optimal_lineup, unfilled_slots
from .valuation import scarcity_report


@dataclass
class Pick:
    overall: int
    round: int
    pick_in_round: int
    team_id: str
    player_id: str | None = None
    player_name: str = ""
    pos: str = ""
    price: float | None = None       # auction leagues

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "round": self.round,
            "pick_in_round": self.pick_in_round,
            "team_id": self.team_id,
            "player_id": self.player_id,
            "player_name": self.player_name,
            "pos": self.pos,
            "price": self.price,
        }


def build_slots(cfg: Mapping[str, Any]) -> list[Pick]:
    """Every pick in the draft, in order, unfilled."""
    order = draft_order(cfg)
    if not order:
        return []
    draft = cfg.get("draft") or {}
    rounds = int(draft.get("rounds") or 15)
    snake = draft.get("type") == "snake"

    slots: list[Pick] = []
    overall = 0
    for rnd in range(1, rounds + 1):
        sequence = list(reversed(order)) if (snake and rnd % 2 == 0) else order
        for i, team_id in enumerate(sequence, start=1):
            overall += 1
            slots.append(Pick(overall=overall, round=rnd, pick_in_round=i, team_id=team_id))
    return slots


class DraftState:
    """Mutable draft in progress."""

    def __init__(self, cfg: Mapping[str, Any], picks: Sequence[Pick] | None = None):
        self.cfg = cfg
        self.slots = build_slots(cfg)
        if picks:
            by_overall = {p.overall: p for p in picks}
            for i, slot in enumerate(self.slots):
                made = by_overall.get(slot.overall)
                if made and made.player_id:
                    self.slots[i] = made

    # -- state ------------------------------------------------------------

    @property
    def made(self) -> list[Pick]:
        return [p for p in self.slots if p.player_id]

    @property
    def on_the_clock(self) -> Pick | None:
        return next((p for p in self.slots if not p.player_id), None)

    def drafted_ids(self) -> set[str]:
        return {p.player_id for p in self.made if p.player_id}

    def roster_ids(self, team_id: str) -> list[str]:
        return [p.player_id for p in self.made if p.team_id == team_id and p.player_id]

    def upcoming_for(self, team_id: str, limit: int = 3) -> list[Pick]:
        return [p for p in self.slots if not p.player_id and p.team_id == team_id][:limit]

    def picks_until_next(self, team_id: str) -> int | None:
        """How many picks happen before this team is up again."""
        pending = [p for p in self.slots if not p.player_id]
        for i, pick in enumerate(pending):
            if pick.team_id == team_id:
                return i
        return None

    # -- mutation ---------------------------------------------------------

    def make_pick(self, player: Player, team_id: str | None = None,
                  price: float | None = None) -> Pick:
        slot = self.on_the_clock
        if slot is None:
            raise ValueError("The draft is already complete.")
        if player.player_id in self.drafted_ids():
            raise ValueError(f"{player.name} has already been drafted.")
        if team_id and team_id != slot.team_id:
            # Allow out-of-order entry (someone picked early / you missed one)
            # by assigning to that team's next open slot instead.
            slot = next(
                (p for p in self.slots if not p.player_id and p.team_id == team_id),
                None,
            )
            if slot is None:
                raise ValueError("That team has no picks left.")

        slot.player_id = player.player_id
        slot.player_name = player.name
        slot.pos = player.pos
        slot.price = price
        return slot

    def undo(self) -> Pick | None:
        """Clear the most recent pick, returning a snapshot of what it was."""
        made = self.made
        if not made:
            return None
        last = made[-1]
        # Snapshot before clearing — the caller needs to report what was undone.
        undone = Pick(
            overall=last.overall,
            round=last.round,
            pick_in_round=last.pick_in_round,
            team_id=last.team_id,
            player_id=last.player_id,
            player_name=last.player_name,
            pos=last.pos,
            price=last.price,
        )
        last.player_id = None
        last.player_name = ""
        last.pos = ""
        last.price = None
        return undone


# --------------------------------------------------------------------------
# Recommendations
# --------------------------------------------------------------------------

def recommend(
    state: DraftState,
    pool: Sequence[Player],
    team_id: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Rank the available players for ``team_id`` right now.

    Score combines three things a good drafter holds in their head at once:

      * VOR — raw value over a freely-available player at the position
      * roster need — a 3rd QB is worth less to you than to the board
      * run risk — how likely this player is gone before your next pick,
        based on ADP and how many picks away that is

    Each is reported separately so you can disagree with the blend.
    """
    cfg = state.cfg
    taken = state.drafted_ids()
    by_id = {p.player_id: p for p in pool}
    roster = [by_id[pid] for pid in state.roster_ids(team_id) if pid in by_id]
    available = [p for p in pool if p.player_id not in taken]
    if not available:
        return []

    gap = state.picks_until_next(team_id)
    capacity_left = roster_capacity(cfg) - len(roster)
    gaps = unfilled_slots(roster, cfg)

    # Lineup value is measured in VOR, not raw points, so an *empty* starting
    # slot is worth replacement level rather than zero. Without this, filling
    # any hole looks infinitely valuable and the board recommends a kicker in
    # round 2. On a full roster the two measures are identical, because the
    # replacement baselines cancel when you swap one starter for another.
    _, current_value = optimal_lineup(roster, cfg, key="vor")

    # The pick that matters for run risk is your *next* one, not this one.
    upcoming = state.upcoming_for(team_id, 2)
    next_overall = upcoming[1].overall if len(upcoming) > 1 else None

    scored = []
    for player in available:
        need = need_multiplier(roster, player.pos, cfg)
        _, with_player = optimal_lineup(list(roster) + [player], cfg, key="vor")
        starter_gain = with_player - current_value

        # Probability this player survives to that pick. ADP is a mean, so
        # treat the window around it as roughly linear.
        if next_overall is None:
            run_risk = 1.0                  # no future pick — it is now or never
        elif player.adp:
            run_risk = 1.0 - _survival_odds(player.adp, next_overall)
        else:
            run_risk = 0.5                  # unknown ADP, assume a coin flip

        # A player who does not crack the starting lineup still has depth
        # value (byes, injuries, upside), scaled down from his full VOR.
        depth = max(0.0, player.vor) * 0.30
        score = (max(0.0, starter_gain) + depth) * need * (1.0 + 0.15 * run_risk)

        scored.append(
            {
                "player": player.to_dict(),
                "score": round(score, 2),
                "vor": round(player.vor, 2),
                "need_multiplier": round(need, 2),
                "starter_gain": round(starter_gain, 2),
                "run_risk": round(run_risk, 2),
                "reason": _reason(player, need, starter_gain, run_risk, gaps),
            }
        )

    scored.sort(key=lambda r: r["score"], reverse=True)

    if capacity_left <= 0:
        return []
    return scored[:limit]


def _survival_odds(adp: float, at_pick: int) -> float:
    """Rough odds a player with this ADP is still on the board at ``at_pick``."""
    spread = max(6.0, adp * 0.18)          # ADP uncertainty widens later
    delta = adp - at_pick
    ratio = 0.5 + delta / (2 * spread)
    return max(0.02, min(0.98, ratio))


def _reason(
    player: Player,
    need: float,
    starter_gain: float,
    run_risk: float,
    gaps: Mapping[str, int],
) -> str:
    bits = []
    if player.value_tier:
        bits.append(f"{player.pos}{player.pos_rank}, tier {player.value_tier}")
    if starter_gain > 0.5:
        bits.append(f"+{starter_gain:.0f} over replacement in your lineup")
    if need > 1.05 and gaps:
        bits.append("fills a starting hole")
    elif need < 0.95:
        bits.append("depth, not a starter")
    if run_risk > 0.7:
        bits.append("unlikely to last")
    elif run_risk < 0.3:
        bits.append("should be there next round")
    return "; ".join(bits)


def board_summary(state: DraftState, pool: Sequence[Player]) -> dict[str, Any]:
    taken = state.drafted_ids()
    available = [p for p in pool if p.player_id not in taken]
    clock = state.on_the_clock
    return {
        "on_the_clock": clock.to_dict() if clock else None,
        "picks_made": len(state.made),
        "picks_total": len(state.slots),
        "scarcity": scarcity_report(available, state.cfg),
        "recent": [p.to_dict() for p in state.made[-8:]][::-1],
    }
