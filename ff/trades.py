"""Trade evaluation and suggestion.

Two questions, one engine:

  * *Is this offer good?* — value both sides, check it against the league's
    configured rules, and report whether each team's starting lineup improves.
  * *What trade should I propose?* — find a position where you have surplus and
    another team has a hole, and vice versa, then search pairings.

Every rule that could differ between leagues (trade deadline, max players per
side, whether uneven trades are legal, how lopsided is too lopsided) is read
from ``cfg["trades"]``. Nothing here is specific to one group.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Mapping, Sequence

from .config import roster_capacity
from .players import Player
from .roster import (
    optimal_lineup,
    position_counts,
    replacement_baselines,
    starting_value,
    unfilled_slots,
)


def _rules(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return cfg.get("trades") or {}


def _fairness(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return _rules(cfg).get("fairness") or {}


def package_value(players: Sequence[Player]) -> float:
    """Total VOR of a package.

    VOR rather than raw points, because giving up your RB2 costs you what a
    waiver-wire RB would *not* have given you.
    """
    return round(sum(p.vor for p in players), 2)


def evaluate(
    cfg: Mapping[str, Any],
    team_a_roster: Sequence[Player],
    team_b_roster: Sequence[Player],
    a_sends: Sequence[Player],
    b_sends: Sequence[Player],
    week: int | None = None,
) -> dict[str, Any]:
    """Score a specific proposal from both sides."""
    a_ids = {p.player_id for p in a_sends}
    b_ids = {p.player_id for p in b_sends}

    a_after = [p for p in team_a_roster if p.player_id not in a_ids] + list(b_sends)
    b_after = [p for p in team_b_roster if p.player_id not in b_ids] + list(a_sends)

    # One currency for both the verdict and the numbers shown beside it:
    # projected points, with an unfilled starting slot priced at replacement
    # level rather than zero. Scoring the verdict in VOR instead used to let a
    # deal be called a win for a team whose displayed points fell, because the
    # VOR-optimal FLEX is not always the points-optimal one.
    baselines = replacement_baselines([*team_a_roster, *team_b_roster])
    a_before_val = starting_value(team_a_roster, cfg, baselines)
    b_before_val = starting_value(team_b_roster, cfg, baselines)
    a_after_val = starting_value(a_after, cfg, baselines)
    b_after_val = starting_value(b_after, cfg, baselines)

    a_gain = round(a_after_val - a_before_val, 2)
    b_gain = round(b_after_val - b_before_val, 2)

    a_out, b_out = package_value(a_sends), package_value(b_sends)
    biggest = max(abs(a_out), abs(b_out), 1.0)
    gap_pct = round(abs(a_out - b_out) / biggest * 100, 1)

    legality = check_legality(
        cfg, a_sends, b_sends, team_a_roster, a_after, team_b_roster, b_after, week
    )
    fairness = _fairness(cfg)
    min_gain = float(fairness.get("min_value_gain", 0.0))

    verdict, notes = _verdict(cfg, a_gain, b_gain, gap_pct, min_gain, legality)
    max_gap = float(fairness.get("max_value_gap_pct", 15.0))

    return {
        "legal": legality["legal"],
        "violations": legality["violations"],
        "verdict": verdict,
        "notes": notes,
        "gap_pct": gap_pct,
        # Legal, but lopsided enough that your league's own guideline says it
        # may draw a veto. Worth knowing before you send the offer.
        "veto_risk": gap_pct > max_gap,
        "team_a": {
            "sends": [p.to_dict() for p in a_sends],
            "receives": [p.to_dict() for p in b_sends],
            "package_out": a_out,
            "package_in": b_out,
            "lineup_before": a_before_val,
            "lineup_after": a_after_val,
            "lineup_gain": a_gain,
        },
        "team_b": {
            "sends": [p.to_dict() for p in b_sends],
            "receives": [p.to_dict() for p in a_sends],
            "package_out": b_out,
            "package_in": a_out,
            "lineup_before": b_before_val,
            "lineup_after": b_after_val,
            "lineup_gain": b_gain,
        },
    }


def _verdict(
    cfg: Mapping[str, Any],
    a_gain: float,
    b_gain: float,
    gap_pct: float,
    min_gain: float,
    legality: Mapping[str, Any],
) -> tuple[str, list[str]]:
    notes: list[str] = []
    fairness = _fairness(cfg)
    max_gap = float(fairness.get("max_value_gap_pct", 15.0))
    both_improve = bool(fairness.get("require_both_improve", True))

    if not legality["legal"]:
        return "illegal", list(legality["violations"])

    if gap_pct > max_gap:
        notes.append(
            f"Value gap is {gap_pct:.0f}%, above your league's {max_gap:.0f}% guideline."
        )

    if a_gain >= min_gain and b_gain >= min_gain:
        notes.append("Both starting lineups improve — the kind of trade that gets approved.")
        verdict = "win-win"
    elif a_gain >= min_gain and b_gain < min_gain:
        notes.append("You gain, they do not — expect this to be declined or vetoed.")
        verdict = "favors-you"
    elif b_gain >= min_gain and a_gain < min_gain:
        notes.append("They gain, you do not.")
        verdict = "favors-them"
    else:
        notes.append("Neither lineup meaningfully improves.")
        verdict = "no-op"

    if both_improve and not (a_gain >= min_gain and b_gain >= min_gain):
        notes.append("Your league's settings expect both teams to improve.")

    approval = _rules(cfg).get("approval")
    if approval == "league_vote" and gap_pct > max_gap:
        votes = _rules(cfg).get("veto_votes_required")
        notes.append(f"Lopsided trades face a league vote ({votes} vetoes kills it).")

    return verdict, notes


def check_legality(
    cfg: Mapping[str, Any],
    a_sends: Sequence[Player],
    b_sends: Sequence[Player],
    a_before: Sequence[Player],
    a_after: Sequence[Player],
    b_before: Sequence[Player],
    b_after: Sequence[Player],
    week: int | None = None,
) -> dict[str, Any]:
    rules = _rules(cfg)
    violations: list[str] = []

    max_side = int(rules.get("max_players_per_side") or 99)
    if len(a_sends) > max_side or len(b_sends) > max_side:
        violations.append(f"League allows at most {max_side} players per side.")

    if not rules.get("allow_uneven", True) and len(a_sends) != len(b_sends):
        violations.append("League does not allow uneven trades.")

    deadline = rules.get("deadline_week")
    if week is not None and deadline and week > int(deadline):
        violations.append(f"Trade deadline was week {deadline}.")

    if rules.get("enforce_roster_limits", True):
        capacity = roster_capacity(cfg)
        if len(a_after) > capacity:
            violations.append(f"Your roster would hold {len(a_after)} of {capacity}.")
        if len(b_after) > capacity:
            violations.append(f"Their roster would hold {len(b_after)} of {capacity}.")
        # Only holes the trade *creates* count against it. A roster that
        # already cannot field a lineup — bye weeks, injuries, a dropped
        # kicker — must still be allowed to trade its way out of trouble,
        # and that is exactly when trading matters most.
        for label, before, after in (
            ("Your", a_before, a_after),
            ("Their", b_before, b_after),
        ):
            existing = unfilled_slots(before, cfg)
            resulting = unfilled_slots(after, cfg)
            worsened = {
                slot: count
                for slot, count in resulting.items()
                if count > existing.get(slot, 0)
            }
            if worsened:
                pretty = ", ".join(f"{n}x {slot}" for slot, n in worsened.items())
                violations.append(
                    f"{label} roster would be left unable to start ({pretty})."
                )

    untouchable = set(rules.get("untouchable_positions") or [])
    for player in list(a_sends) + list(b_sends):
        if player.pos in untouchable:
            violations.append(f"{player.pos} cannot be traded in this league.")
            break

    return {"legal": not violations, "violations": violations}


# --------------------------------------------------------------------------
# Suggestion engine
# --------------------------------------------------------------------------

def surplus_and_needs(
    roster: Sequence[Player], cfg: Mapping[str, Any]
) -> tuple[dict[str, list[Player]], list[str]]:
    """Which positions a roster can afford to trade from, and which it needs.

    Starters are decided on VOR here even though ``evaluate`` scores deals in
    points, because this asks a different question: not "who scores most this
    week" but "who is an asset worth keeping". Deciding it on raw points would
    call a 240-point RB a starter over a 180-point WR even in a league where
    the WR is the scarcer asset — and then look for surplus in the wrong place.
    """
    lineup, _ = optimal_lineup(roster, cfg, key="vor")
    starters = {id(p) for group in lineup.values() for p in group}

    surplus: dict[str, list[Player]] = {}
    for player in roster:
        if id(player) not in starters and player.vor > 0:
            surplus.setdefault(player.pos, []).append(player)
    for players in surplus.values():
        players.sort(key=lambda p: p.vor, reverse=True)

    counts = position_counts(roster)
    needs = []
    for slot in (cfg.get("roster") or {}).get("slots", []):
        for pos in slot.get("eligible") or []:
            starters_at_pos = len(
                [p for p in roster if p.pos == pos and id(p) in starters]
            )
            if counts.get(pos, 0) <= starters_at_pos and pos not in needs:
                needs.append(pos)

    weakest = sorted(
        {p.pos for p in roster} | set(needs),
        key=lambda pos: max(
            (p.vor for p in roster if p.pos == pos), default=-999.0
        ),
    )
    for pos in weakest:
        if pos not in needs:
            needs.append(pos)

    return surplus, needs


def suggest(
    cfg: Mapping[str, Any],
    my_roster: Sequence[Player],
    other_rosters: Mapping[str, Sequence[Player]],
    week: int | None = None,
    limit: int = 10,
    max_per_side: int | None = None,
    partner: str | None = None,
) -> list[dict[str, Any]]:
    """Generate trades worth proposing, best first.

    Searches every 1-for-1 and (where legal) 2-for-1 pairing between your
    surplus and each partner's, keeping only deals that are legal under your
    league's rules and that improve both starting lineups.

    Results are spread across partners rather than ranked purely by your own
    gain: one rival whose surplus happens to match your holes can otherwise
    produce every deal in the list, and you never learn the other nine teams
    had anything to offer. Pass ``partner`` to drill into a single team, which
    does rank strictly best-first.
    """
    if partner is not None:
        other_rosters = {k: v for k, v in other_rosters.items() if k == partner}

    rules = _rules(cfg)
    cap = int(max_per_side or rules.get("max_players_per_side") or 2)
    cap = max(1, min(cap, 3))
    allow_uneven = bool(rules.get("allow_uneven", True))
    min_gain = float(_fairness(cfg).get("min_value_gain", 0.0))

    my_surplus, my_needs = surplus_and_needs(my_roster, cfg)
    my_candidates = _tradeable(my_roster, my_surplus)

    results: list[dict[str, Any]] = []

    for team_id, roster in other_rosters.items():
        their_surplus, their_needs = surplus_and_needs(roster, cfg)
        their_candidates = _tradeable(roster, their_surplus)

        for a_sends in _packages(my_candidates, cap, their_needs):
            for b_sends in _packages(their_candidates, cap, my_needs):
                if not allow_uneven and len(a_sends) != len(b_sends):
                    continue
                result = evaluate(cfg, my_roster, roster, a_sends, b_sends, week)
                if not result["legal"]:
                    continue
                a_gain = result["team_a"]["lineup_gain"]
                b_gain = result["team_b"]["lineup_gain"]
                if a_gain < max(min_gain, 0.01) or b_gain < max(min_gain, 0.01):
                    continue
                result["partner_team_id"] = team_id
                result["combined_gain"] = round(a_gain + b_gain, 2)
                results.append(result)

    # Deals that would survive a veto vote come first — a bigger gain you can
    # never get approved is worth less than a smaller one you can.
    results.sort(
        key=lambda r: (
            not r["veto_risk"],
            r["team_a"]["lineup_gain"],
            r["combined_gain"],
        ),
        reverse=True,
    )
    ranked = _dedupe(results)
    if partner is not None:
        return ranked[:limit]
    return _spread_across_partners(ranked)[:limit]


def _spread_across_partners(ranked: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Your best offer to each rival, then your second-best to each, and so on.

    ``ranked`` is already in best-first order and grouping preserves it, so
    every team's list stays correctly sorted; only the interleaving is new.
    Partners are ordered by their own best deal, so the single strongest offer
    is still the first thing you see.
    """
    by_partner: dict[str, list[dict[str, Any]]] = {}
    for result in ranked:
        by_partner.setdefault(result["partner_team_id"], []).append(result)
    if not by_partner:
        return []

    order = sorted(by_partner, key=lambda t: -by_partner[t][0]["team_a"]["lineup_gain"])
    out: list[dict[str, Any]] = []
    for rank in range(max(len(deals) for deals in by_partner.values())):
        for team_id in order:
            if rank < len(by_partner[team_id]):
                out.append(by_partner[team_id][rank])
    return out


def _tradeable(
    roster: Sequence[Player], surplus: Mapping[str, list[Player]]
) -> list[Player]:
    """Surplus first, then mid-value starters — stars stay put."""
    out = [p for players in surplus.values() for p in players]
    seen = {p.player_id for p in out}
    ranked = sorted(roster, key=lambda p: p.vor, reverse=True)
    # Skip each team's top two assets; nobody trades those in a fair deal.
    for player in ranked[2:]:
        if player.player_id not in seen:
            seen.add(player.player_id)
            out.append(player)
    return out[:14]


def _packages(
    candidates: Sequence[Player], cap: int, wanted: Sequence[str]
) -> list[tuple[Player, ...]]:
    """Candidate packages, preferring players the other side actually needs."""
    focused = [p for p in candidates if p.pos in wanted] or list(candidates)
    focused = focused[:10]
    packages: list[tuple[Player, ...]] = [(p,) for p in focused]
    if cap >= 2:
        packages.extend(combinations(focused[:7], 2))
    return packages


def _dedupe(results: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out = []
    for result in results:
        key = (
            result.get("partner_team_id"),
            tuple(sorted(p["player_id"] for p in result["team_a"]["sends"])),
            tuple(sorted(p["player_id"] for p in result["team_a"]["receives"])),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(result)
    return out
