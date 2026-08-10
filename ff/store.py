"""SQLite persistence for everything that changes during a season.

League *rules* live in JSON under ``leagues/`` because you edit and share them.
League *state* — draft picks, rosters, saved trades — lives here because it
changes constantly and benefits from transactions.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from .draft import Pick
from .paths import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    league_id   TEXT NOT NULL,
    overall     INTEGER NOT NULL,
    round       INTEGER NOT NULL,
    pick_in_round INTEGER NOT NULL,
    team_id     TEXT NOT NULL,
    player_id   TEXT,
    player_name TEXT,
    pos         TEXT,
    price       REAL,
    PRIMARY KEY (league_id, overall)
);

CREATE TABLE IF NOT EXISTS rosters (
    league_id TEXT NOT NULL,
    team_id   TEXT NOT NULL,
    player_id TEXT NOT NULL,
    acquired  TEXT DEFAULT 'draft',
    PRIMARY KEY (league_id, team_id, player_id)
);

CREATE TABLE IF NOT EXISTS saved_trades (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id TEXT NOT NULL,
    created   TEXT NOT NULL,
    payload   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    league_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    PRIMARY KEY (league_id, key)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --- draft picks ---------------------------------------------------------

def save_picks(conn: sqlite3.Connection, league_id: str, picks: Iterable[Pick]) -> None:
    rows = [
        (
            league_id, p.overall, p.round, p.pick_in_round, p.team_id,
            p.player_id, p.player_name, p.pos, p.price,
        )
        for p in picks
        if p.player_id
    ]
    with conn:
        conn.execute("DELETE FROM picks WHERE league_id = ?", (league_id,))
        conn.executemany(
            "INSERT INTO picks (league_id, overall, round, pick_in_round, team_id,"
            " player_id, player_name, pos, price) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )


def load_picks(conn: sqlite3.Connection, league_id: str) -> list[Pick]:
    cursor = conn.execute(
        "SELECT * FROM picks WHERE league_id = ? ORDER BY overall", (league_id,)
    )
    return [
        Pick(
            overall=row["overall"],
            round=row["round"],
            pick_in_round=row["pick_in_round"],
            team_id=row["team_id"],
            player_id=row["player_id"],
            player_name=row["player_name"] or "",
            pos=row["pos"] or "",
            price=row["price"],
        )
        for row in cursor
    ]


def clear_draft(conn: sqlite3.Connection, league_id: str) -> None:
    with conn:
        conn.execute("DELETE FROM picks WHERE league_id = ?", (league_id,))


# --- rosters -------------------------------------------------------------

def set_roster(
    conn: sqlite3.Connection, league_id: str, team_id: str, player_ids: Sequence[str]
) -> None:
    with conn:
        conn.execute(
            "DELETE FROM rosters WHERE league_id = ? AND team_id = ?",
            (league_id, team_id),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO rosters (league_id, team_id, player_id)"
            " VALUES (?,?,?)",
            [(league_id, team_id, pid) for pid in player_ids],
        )


def load_rosters(conn: sqlite3.Connection, league_id: str) -> dict[str, list[str]]:
    cursor = conn.execute(
        "SELECT team_id, player_id FROM rosters WHERE league_id = ?", (league_id,)
    )
    out: dict[str, list[str]] = {}
    for row in cursor:
        out.setdefault(row["team_id"], []).append(row["player_id"])
    return out


def seed_rosters_from_draft(conn: sqlite3.Connection, league_id: str) -> int:
    """Turn a completed draft into starting rosters."""
    picks = load_picks(conn, league_id)
    by_team: dict[str, list[str]] = {}
    for pick in picks:
        if pick.player_id:
            by_team.setdefault(pick.team_id, []).append(pick.player_id)
    for team_id, ids in by_team.items():
        set_roster(conn, league_id, team_id, ids)
    return sum(len(v) for v in by_team.values())


# --- misc ----------------------------------------------------------------

def save_trade(conn: sqlite3.Connection, league_id: str, payload: dict[str, Any],
               created: str) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO saved_trades (league_id, created, payload) VALUES (?,?,?)",
            (league_id, created, json.dumps(payload)),
        )
    return int(cursor.lastrowid or 0)


def list_trades(conn: sqlite3.Connection, league_id: str) -> list[dict[str, Any]]:
    cursor = conn.execute(
        "SELECT id, created, payload FROM saved_trades WHERE league_id = ?"
        " ORDER BY id DESC",
        (league_id,),
    )
    return [
        {"id": row["id"], "created": row["created"], **json.loads(row["payload"])}
        for row in cursor
    ]


def put(conn: sqlite3.Connection, league_id: str, key: str, value: Any) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv (league_id, key, value) VALUES (?,?,?)",
            (league_id, key, json.dumps(value)),
        )


def get(conn: sqlite3.Connection, league_id: str, key: str, default: Any = None) -> Any:
    row = conn.execute(
        "SELECT value FROM kv WHERE league_id = ? AND key = ?", (league_id, key)
    ).fetchone()
    return json.loads(row["value"]) if row else default
