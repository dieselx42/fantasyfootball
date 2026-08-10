"""League configuration: load, validate, and supply defaults.

Everything that differs between fantasy leagues lives in a single JSON file
under ``leagues/``. No league rule is hardcoded anywhere else in this package.
To run a different group's league, point the app at a different file.

A config is a plain dict so it round-trips to JSON and to the web UI editor
without a translation layer. :func:`validate` is the single gatekeeper.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .scoring_vocab import is_stat

LEAGUES_DIR = Path(__file__).resolve().parent.parent / "leagues"

#: Positions the valuation engine understands. FLEX-style slots are defined in
#: the roster block and reference these.
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")

SCHEMA_VERSION = 1


class ConfigError(ValueError):
    """Raised when a league config is structurally invalid."""


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

def default_scoring(ppr: float = 0.5) -> dict[str, float]:
    """Standard scoring with a configurable per-reception value.

    ``ppr=0`` standard, ``0.5`` half-PPR, ``1`` full PPR.
    """
    return {
        "pass_yd": 0.04,
        "pass_td": 4,
        "pass_int": -2,
        "pass_2pt": 2,
        "rush_yd": 0.1,
        "rush_td": 6,
        "rush_2pt": 2,
        "rec": ppr,
        "rec_yd": 0.1,
        "rec_td": 6,
        "rec_2pt": 2,
        "fum_lost": -2,
        "ret_td": 6,
        "fg_0_19": 3,
        "fg_20_29": 3,
        "fg_30_39": 3,
        "fg_40_49": 4,
        "fg_50p": 5,
        "fg_miss": -1,
        "xp_made": 1,
        "xp_miss": -1,
        "dst_sack": 1,
        "dst_int": 2,
        "dst_fum_rec": 2,
        "dst_td": 6,
        "dst_safety": 2,
        "dst_block": 2,
    }


def default_roster() -> dict[str, Any]:
    return {
        "slots": [
            {"slot": "QB", "count": 1, "eligible": ["QB"]},
            {"slot": "RB", "count": 2, "eligible": ["RB"]},
            {"slot": "WR", "count": 2, "eligible": ["WR"]},
            {"slot": "TE", "count": 1, "eligible": ["TE"]},
            {"slot": "FLEX", "count": 1, "eligible": ["RB", "WR", "TE"]},
            {"slot": "K", "count": 1, "eligible": ["K"]},
            {"slot": "DST", "count": 1, "eligible": ["DST"]},
        ],
        "bench": 6,
        "ir": 1,
    }


def default_trade_rules() -> dict[str, Any]:
    return {
        # How a trade becomes official.
        "approval": "commissioner",      # commissioner | league_vote | instant
        "veto_votes_required": 4,        # only used when approval == league_vote
        "review_period_hours": 24,
        "deadline_week": 11,
        # Shape constraints.
        "max_players_per_side": 3,
        "allow_uneven": True,            # 2-for-1 etc.
        "allow_draft_picks": False,
        "allow_faab": False,
        "enforce_roster_limits": True,
        # What the suggestion engine considers a *sane* offer. These do not
        # block a trade in your league — they steer what gets proposed.
        "fairness": {
            "max_value_gap_pct": 15.0,   # reject lopsided proposals
            "require_both_improve": True,  # both starting lineups must gain
            "min_value_gain": 1.0,       # ignore noise-level "upgrades"
        },
        "untouchable_positions": [],
    }


def default_valuation() -> dict[str, Any]:
    return {
        "method": "vor",                 # vor | points
        # "auto" derives replacement level from league size x starters at the
        # position (including the share of FLEX that position typically wins).
        "replacement": "auto",
        "flex_share": {"RB": 0.45, "WR": 0.45, "TE": 0.10},
        # How much to weight scarcity vs raw points when recommending picks.
        "scarcity_weight": 1.0,
        # Roster-need multiplier applied on the draft board once a team has
        # filled its starters at a position.
        "need_weight": 0.25,
        "tier_break_sensitivity": 1.0,
    }


def new_config(
    name: str = "My League",
    platform: str = "manual",
    team_count: int = 12,
    ppr: float = 0.5,
) -> dict[str, Any]:
    """Build a complete, valid config with sensible defaults."""
    return {
        "schema_version": SCHEMA_VERSION,
        "id": slugify(name),
        "name": name,
        "season": 2025,
        "platform": {"kind": platform, "league_id": "", "settings": {}},
        "team_count": team_count,
        "scoring": default_scoring(ppr),
        "scoring_bonuses": [],
        "roster": default_roster(),
        "draft": {
            "type": "snake",             # snake | linear | auction
            "rounds": 15,
            "seconds_per_pick": 90,
            "order": [],                 # list of team ids; empty = order of `teams`
            "auction_budget": 200,
        },
        "valuation": default_valuation(),
        "trades": default_trade_rules(),
        "teams": [],
        "my_team_id": None,
    }


def new_team(name: str, manager: str = "") -> dict[str, Any]:
    return {"id": slugify(name), "name": name, "manager": manager}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "league"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate(cfg: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems. Empty list means valid."""
    problems: list[str] = []

    def require(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    require(bool(str(cfg.get("name", "")).strip()), "League needs a name.")
    require(
        isinstance(cfg.get("team_count"), int) and 2 <= cfg["team_count"] <= 32,
        "Team count must be a whole number between 2 and 32.",
    )

    # --- scoring ---------------------------------------------------------
    scoring = cfg.get("scoring")
    if not isinstance(scoring, dict) or not scoring:
        problems.append("Scoring rules are missing.")
    else:
        for key, value in scoring.items():
            if not is_stat(key):
                problems.append(f"Unknown scoring stat '{key}'.")
            elif not isinstance(value, (int, float)):
                problems.append(f"Scoring value for '{key}' must be a number.")

    for i, bonus in enumerate(cfg.get("scoring_bonuses") or []):
        where = f"Bonus #{i + 1}"
        if not is_stat(bonus.get("stat", "")):
            problems.append(f"{where}: unknown stat '{bonus.get('stat')}'.")
        if not isinstance(bonus.get("threshold"), (int, float)):
            problems.append(f"{where}: threshold must be a number.")
        if not isinstance(bonus.get("points"), (int, float)):
            problems.append(f"{where}: points must be a number.")

    # --- roster ----------------------------------------------------------
    roster = cfg.get("roster") or {}
    slots = roster.get("slots")
    if not isinstance(slots, list) or not slots:
        problems.append("Roster must define at least one starting slot.")
    else:
        for slot in slots:
            label = slot.get("slot", "?")
            if not isinstance(slot.get("count"), int) or slot["count"] < 0:
                problems.append(f"Slot '{label}': count must be 0 or more.")
            eligible = slot.get("eligible") or []
            if not eligible:
                problems.append(f"Slot '{label}': needs at least one eligible position.")
            for pos in eligible:
                if pos not in POSITIONS:
                    problems.append(f"Slot '{label}': unknown position '{pos}'.")
    if not isinstance(roster.get("bench", 0), int) or roster.get("bench", 0) < 0:
        problems.append("Bench size must be 0 or more.")

    # --- draft -----------------------------------------------------------
    draft = cfg.get("draft") or {}
    if draft.get("type") not in ("snake", "linear", "auction"):
        problems.append("Draft type must be snake, linear or auction.")
    if not isinstance(draft.get("rounds"), int) or draft["rounds"] < 1:
        problems.append("Draft rounds must be at least 1.")

    # --- teams -----------------------------------------------------------
    teams = cfg.get("teams") or []
    ids = [t.get("id") for t in teams]
    if len(ids) != len(set(ids)):
        problems.append("Two teams share the same id.")
    for team in teams:
        if not str(team.get("name", "")).strip():
            problems.append("Every team needs a name.")
    if teams and len(teams) != cfg.get("team_count"):
        problems.append(
            f"You have {len(teams)} teams listed but team count is "
            f"{cfg.get('team_count')}."
        )
    if cfg.get("my_team_id") and cfg["my_team_id"] not in ids:
        problems.append("'My team' points at a team that is not in the list.")

    order = draft.get("order") or []
    if order:
        if sorted(order) != sorted(ids):
            problems.append("Draft order must list every team exactly once.")

    # --- trades ----------------------------------------------------------
    trades = cfg.get("trades") or {}
    if trades.get("approval") not in ("commissioner", "league_vote", "instant"):
        problems.append("Trade approval must be commissioner, league_vote or instant.")
    gap = (trades.get("fairness") or {}).get("max_value_gap_pct")
    if gap is not None and not (0 <= float(gap) <= 100):
        problems.append("Trade fairness gap must be between 0 and 100 percent.")

    return problems


def ensure_valid(cfg: dict[str, Any]) -> None:
    problems = validate(cfg)
    if problems:
        raise ConfigError("; ".join(problems))


# --------------------------------------------------------------------------
# Derived views (used by valuation + draft)
# --------------------------------------------------------------------------

def starters_by_position(cfg: dict[str, Any]) -> dict[str, float]:
    """Starting spots per position per team, spreading FLEX by configured share.

    Returns fractional counts — a 1-FLEX league with the default share gives
    RB 2.45, WR 2.45, TE 1.10. This drives replacement level, which is the
    number that makes a 300-point RB and a 300-point QB comparable.
    """
    share = (cfg.get("valuation") or {}).get("flex_share") or {}
    counts = {pos: 0.0 for pos in POSITIONS}

    for slot in (cfg.get("roster") or {}).get("slots", []):
        eligible = slot.get("eligible") or []
        count = float(slot.get("count") or 0)
        if count <= 0:
            continue
        if len(eligible) == 1:
            counts[eligible[0]] += count
            continue
        # Multi-position (FLEX / superflex) slot: distribute by share, falling
        # back to an even split for positions the config does not weight.
        weights = {pos: float(share.get(pos, 0.0)) for pos in eligible}
        total = sum(weights.values())
        if total <= 0:
            weights = {pos: 1.0 for pos in eligible}
            total = float(len(eligible))
        for pos in eligible:
            counts[pos] += count * weights[pos] / total

    return counts


def roster_capacity(cfg: dict[str, Any]) -> int:
    """Total roster spots per team, starters + bench (IR excluded)."""
    roster = cfg.get("roster") or {}
    starters = sum(int(s.get("count") or 0) for s in roster.get("slots", []))
    return starters + int(roster.get("bench") or 0)


def draft_order(cfg: dict[str, Any]) -> list[str]:
    order = (cfg.get("draft") or {}).get("order") or []
    if order:
        return list(order)
    return [t["id"] for t in cfg.get("teams", [])]


def team_by_id(cfg: dict[str, Any], team_id: str) -> dict[str, Any] | None:
    for team in cfg.get("teams", []):
        if team.get("id") == team_id:
            return team
    return None


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def config_path(league_id: str) -> Path:
    return LEAGUES_DIR / f"{league_id}.json"


def load(league_id: str) -> dict[str, Any]:
    path = config_path(league_id)
    if not path.exists():
        raise ConfigError(f"No league config at {path}")
    return load_file(path)


def load_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        cfg = json.load(handle)
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path} does not contain a league object.")
    return migrate(cfg)


def save(cfg: dict[str, Any]) -> Path:
    ensure_valid(cfg)
    LEAGUES_DIR.mkdir(parents=True, exist_ok=True)
    path = config_path(cfg["id"])
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2, sort_keys=False)
        handle.write("\n")
    tmp.replace(path)
    return path


def list_leagues() -> list[dict[str, str]]:
    if not LEAGUES_DIR.exists():
        return []
    out = []
    for path in sorted(LEAGUES_DIR.glob("*.json")):
        try:
            cfg = load_file(path)
        except (ConfigError, json.JSONDecodeError):
            continue
        out.append(
            {
                "id": cfg.get("id", path.stem),
                "name": cfg.get("name", path.stem),
                "platform": (cfg.get("platform") or {}).get("kind", "manual"),
                "teams": str(len(cfg.get("teams") or [])),
            }
        )
    return out


def migrate(cfg: dict[str, Any]) -> dict[str, Any]:
    """Fill in anything a config written by an older version is missing."""
    base = new_config(cfg.get("name", "My League"))
    for key, value in base.items():
        cfg.setdefault(key, value)
    cfg.setdefault("id", slugify(cfg["name"]))
    # Nested blocks get the same treatment so new knobs land with defaults.
    for block, defaults in (
        ("valuation", default_valuation()),
        ("trades", default_trade_rules()),
    ):
        merged = dict(defaults)
        merged.update(cfg.get(block) or {})
        if block == "trades":
            fairness = dict(defaults["fairness"])
            fairness.update((cfg.get(block) or {}).get("fairness") or {})
            merged["fairness"] = fairness
        cfg[block] = merged
    cfg["schema_version"] = SCHEMA_VERSION
    return cfg
