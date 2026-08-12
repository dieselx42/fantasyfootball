"""The player pool: projections in, scored+identified players out.

Projections come from whatever source you have. The importer is forgiving
about column names because every site exports a slightly different header row.
Drop a CSV in ``data/projections/`` and it will be picked up.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .paths import PROJECTIONS_DIR
from .scoring import score_stats
from .scoring_vocab import ALL_STATS

#: Header aliases seen in the wild -> canonical stat key.
COLUMN_ALIASES: dict[str, str] = {
    # identity
    "player": "name", "player name": "name", "name": "name", "full name": "name",
    "team": "team", "tm": "team", "nfl team": "team",
    "pos": "pos", "position": "pos",
    "bye": "bye", "bye week": "bye",
    "week": "week", "wk": "week",
    "opp": "opponent", "opponent": "opponent", "vs": "opponent",
    "status": "status", "injury": "status", "injury status": "status",
    "game status": "status", "practice status": "status",
    "adp": "adp", "avg pick": "adp", "average draft position": "adp",
    "rank": "rank", "ovr": "rank", "overall": "rank",
    "tier": "tier",
    "points": "points", "fpts": "points", "proj pts": "points",
    "projected points": "points", "fantasy points": "points",
    # passing
    "pass yds": "pass_yd", "passing yards": "pass_yd", "pass yards": "pass_yd",
    "py": "pass_yd", "pyds": "pass_yd",
    "pass td": "pass_td", "passing td": "pass_td", "ptd": "pass_td",
    "pass tds": "pass_td", "passing tds": "pass_td",
    "int": "pass_int", "ints": "pass_int", "interceptions": "pass_int",
    "cmp": "pass_cmp", "completions": "pass_cmp",
    "att": "pass_att", "pass att": "pass_att", "attempts": "pass_att",
    # rushing
    "rush yds": "rush_yd", "rushing yards": "rush_yd", "ry": "rush_yd",
    "rush yards": "rush_yd", "ryds": "rush_yd",
    "rush td": "rush_td", "rushing td": "rush_td", "rtd": "rush_td",
    "rush tds": "rush_td", "rushing tds": "rush_td",
    "rush att": "rush_att", "carries": "rush_att",
    # receiving
    "rec": "rec", "receptions": "rec", "catches": "rec",
    "rec yds": "rec_yd", "receiving yards": "rec_yd", "recyds": "rec_yd",
    "rec yards": "rec_yd",
    "rec td": "rec_td", "receiving td": "rec_td", "rec tds": "rec_td",
    "receiving tds": "rec_td",
    "tgt": "rec_tgt", "targets": "rec_tgt",
    # misc
    "fl": "fum_lost", "fum lost": "fum_lost", "fumbles lost": "fum_lost",
    "fum": "fum", "fumbles": "fum",
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

TEAM_FIXES = {"JAX": "JAC", "WSH": "WAS", "LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR"}

#: Availability codes, canonicalised from the many spellings sites use.
#: ``BYE`` is derived rather than reported, but shares the same vocabulary so
#: one field answers "can I start this player?".
STATUS_ALIASES = {
    "": "", "a": "", "active": "", "healthy": "", "p": "", "probable": "",
    "q": "Q", "questionable": "Q", "gtd": "Q", "game time decision": "Q",
    "d": "D", "doubtful": "D",
    "o": "O", "out": "O", "inactive": "O",
    "ir": "IR", "injured reserve": "IR", "ir-r": "IR", "pup": "IR", "nfi": "IR",
    "sus": "SUS", "susp": "SUS", "suspended": "SUS",
    "na": "NA", "n/a": "NA", "not active": "NA", "dnr": "NA",
    "bye": "BYE",
}

#: What share of a player's projection survives their status. Questionable
#: players do play, usually on reduced snaps; doubtful ones rarely do. Out,
#: IR and suspended score nothing, which is what makes the lineup optimiser
#: bench them without any special-case code.
STATUS_MULTIPLIERS = {
    "": 1.0, "Q": 0.92, "D": 0.25,
    "O": 0.0, "IR": 0.0, "SUS": 0.0, "NA": 0.0, "BYE": 0.0,
}


def normalize_status(status: str) -> str:
    key = re.sub(r"\s+", " ", (status or "").strip().lower())
    if key in STATUS_ALIASES:
        return STATUS_ALIASES[key]
    upper = key.upper()
    return upper if upper in STATUS_MULTIPLIERS else ""


def status_multiplier(status: str, cfg: Mapping[str, Any] | None = None) -> float:
    """How much of a projection a status leaves behind.

    Overridable per league under ``valuation.status_multipliers`` — some people
    want a questionable player treated as fully available, some want zero.
    """
    overrides = ((cfg or {}).get("valuation") or {}).get("status_multipliers") or {}
    code = normalize_status(status)
    if code in overrides:
        return float(overrides[code])
    return STATUS_MULTIPLIERS.get(code, 1.0)


def normalize_name(name: str) -> str:
    """A join key that survives 'A.J.' vs 'AJ' and 'Jr.' vs 'Jr'."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", "and")
    text = re.sub(r"[^a-z0-9\s]", "", text)
    parts = [p for p in text.split() if p not in _SUFFIXES]
    return " ".join(parts)


def normalize_pos(pos: str) -> str:
    pos = (pos or "").strip().upper()
    pos = re.sub(r"\d+$", "", pos)          # "RB1" -> "RB"
    if pos in ("D/ST", "DEF", "D", "DST"):
        return "DST"
    if pos in ("PK", "K"):
        return "K"
    return pos


def normalize_team(team: str) -> str:
    team = (team or "").strip().upper()
    return TEAM_FIXES.get(team, team)


@dataclass
class Player:
    """One projectable player, already scored for the active league."""

    player_id: str
    name: str
    pos: str
    team: str = ""
    bye: int | None = None
    points: float = 0.0
    stats: dict[str, float] = field(default_factory=dict)
    adp: float | None = None
    rank: int | None = None
    tier: int | None = None
    source: str = ""
    # Weekly context. ``week`` is None for a season-long projection.
    week: int | None = None
    status: str = ""
    opponent: str = ""
    #: The season-long projection this player's weekly number came from, kept
    #: alongside it so one list answers both "best lineup this week" and "best
    #: player to hold" without a parallel data structure.
    season_points: float = 0.0
    # Filled in by the valuation engine.
    vor: float = 0.0
    pos_rank: int = 0
    value_tier: int = 0

    @property
    def playable(self) -> bool:
        """Whether starting this player could score anything at all."""
        return STATUS_MULTIPLIERS.get(self.status, 1.0) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "name": self.name,
            "pos": self.pos,
            "team": self.team,
            "bye": self.bye,
            "points": round(self.points, 2),
            "adp": self.adp,
            "rank": self.rank,
            "tier": self.tier,
            "vor": round(self.vor, 2),
            "pos_rank": self.pos_rank,
            "value_tier": self.value_tier,
            "source": self.source,
            "week": self.week,
            "status": self.status,
            "opponent": self.opponent,
            "season_points": round(self.season_points, 2),
            "playable": self.playable,
            "stats": self.stats,
        }


def make_player_id(name: str, pos: str, team: str) -> str:
    return f"{normalize_name(name).replace(' ', '-')}-{normalize_pos(pos)}".strip("-")


# --------------------------------------------------------------------------
# CSV import
# --------------------------------------------------------------------------

def _canonical_header(header: str) -> str | None:
    key = re.sub(r"\s+", " ", (header or "").strip().lower())
    key = key.replace("_", " ").replace(".", "")
    if key in COLUMN_ALIASES:
        return COLUMN_ALIASES[key]
    compact = key.replace(" ", "_")
    if compact in ALL_STATS:
        return compact
    return None


def parse_projection_csv(text: str, source: str = "csv") -> list[Player]:
    """Parse a projections CSV into scored-later :class:`Player` objects.

    Unknown columns are ignored rather than fatal — exports carry a lot of
    noise, and refusing to import because of one strange column would be a bad
    trade the night before a draft.
    """
    # Some exports prepend a title line before the real header.
    sample = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(sample))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return []

    header_idx = 0
    for i, row in enumerate(rows[:5]):
        if sum(1 for cell in row if _canonical_header(cell)) >= 2:
            header_idx = i
            break

    headers = [_canonical_header(cell) for cell in rows[header_idx]]
    players: list[Player] = []

    for row in rows[header_idx + 1:]:
        record: dict[str, Any] = {}
        for cell, key in zip(row, headers):
            if key is None:
                continue
            value = cell.strip().replace(",", "")
            if value == "":
                continue
            record[key] = value

        name = str(record.get("name", "")).strip()
        if not name:
            continue
        pos = normalize_pos(str(record.get("pos", "")))
        team = normalize_team(str(record.get("team", "")))

        # Many exports pack team into the name: "Ja'Marr Chase CIN".
        if not team:
            match = re.search(r"\(([A-Z]{2,3})\)\s*$|\s+([A-Z]{2,3})$", name)
            if match:
                team = normalize_team(match.group(1) or match.group(2))
                name = name[: match.start()].strip()

        stats = {
            key: _as_float(value)
            for key, value in record.items()
            if key in ALL_STATS
        }

        players.append(
            Player(
                player_id=make_player_id(name, pos, team),
                name=name,
                pos=pos,
                team=team,
                bye=_as_int(record.get("bye")),
                points=_as_float(record.get("points")),
                stats=stats,
                adp=_as_float(record.get("adp")) or None,
                rank=_as_int(record.get("rank")),
                tier=_as_int(record.get("tier")),
                source=source,
                week=_as_int(record.get("week")),
                status=normalize_status(str(record.get("status", ""))),
                opponent=str(record.get("opponent", "")).strip().upper(),
            )
        )

    return players


def _as_float(value: Any) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Pool assembly
# --------------------------------------------------------------------------

def score_pool(players: Iterable[Player], cfg: Mapping[str, Any]) -> list[Player]:
    """Apply league scoring.

    A player's own stat line always wins. Only when a source gives no usable
    stats do we fall back to its precomputed ``points`` column — that number
    was computed under *someone else's* scoring rules, so it is a last resort
    and is flagged as such.
    """
    out = []
    for player in players:
        if player.stats:
            player.points = score_stats(player.stats, cfg)
        elif player.points:
            player.source = (player.source or "") + " (source points)"
        out.append(player)
    return out


def merge_pools(*pools: Iterable[Player]) -> list[Player]:
    """Combine sources, first pool wins on conflict, later ones fill gaps.

    Keyed by player *and week*, so a season-long file and a week 3 file can
    both be loaded without one overwriting the other.
    """
    merged: dict[tuple[str, int | None], Player] = {}
    for pool in pools:
        for player in pool:
            key = (player.player_id, player.week)
            existing = merged.get(key)
            if existing is None:
                merged[key] = player
                continue
            for attr in ("team", "bye", "adp", "rank", "tier", "status", "opponent"):
                if getattr(existing, attr) in (None, "", 0):
                    setattr(existing, attr, getattr(player, attr))
            if not existing.stats and player.stats:
                existing.stats = player.stats
            if not existing.points and player.points:
                existing.points = player.points
    return list(merged.values())


def available_projection_files() -> list[Path]:
    """File-backend only; kept for tooling and tests."""
    if not PROJECTIONS_DIR.exists():
        return []
    return sorted(PROJECTIONS_DIR.glob("*.csv"))


def load_projections(
    paths: Iterable[Path] | None = None, week: int | None = None
) -> list[Player]:
    """Every stored projection set, merged.

    With no argument this reads through the active storage backend, so it
    works identically against local CSVs and a hosted database.

    ``week`` selects which rows come back. The default, ``None``, returns only
    season-long rows — a file with no ``week`` column behaves exactly as it
    always has. Passing a number returns only rows carrying that week.
    """
    if paths is not None:
        pools = [
            parse_projection_csv(p.read_text(encoding="utf-8"), p.stem)
            for p in paths
        ]
    else:
        from .storage import get_backend

        pools = [
            parse_projection_csv(text, name)
            for name, text in get_backend().load_projection_texts()
        ]

    merged = merge_pools(*pools) if pools else []
    return [p for p in merged if p.week == week]
