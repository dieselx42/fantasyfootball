"""Local-disk backend: JSON league configs, CSV projections, SQLite state.

This is the zero-dependency path. ``python3 run.py`` on a laptop uses it, which
is what keeps draft night free of venvs and package installs.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .. import paths
from .base import Backend

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
"""


class FileBackend(Backend):
    name = "files"

    def __init__(self, *, leagues_dir: Path | None = None,
                 projections_dir: Path | None = None,
                 db_path: Path | None = None,
                 secrets_dir: Path | None = None):
        self.leagues_dir = leagues_dir or paths.LEAGUES_DIR
        self.projections_dir = projections_dir or paths.PROJECTIONS_DIR
        self.db_path = db_path or paths.DB_PATH
        self.secrets_dir = secrets_dir or paths.SECRETS_DIR

    # -- sqlite -----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn

    # -- leagues ----------------------------------------------------------

    def _league_path(self, league_id: str) -> Path:
        return self.leagues_dir / f"{Path(league_id).name}.json"

    def list_leagues(self) -> list[dict[str, Any]]:
        if not self.leagues_dir.exists():
            return []
        out = []
        for path in sorted(self.leagues_dir.glob("*.json")):
            try:
                cfg = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
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

    def load_league(self, league_id: str) -> dict[str, Any] | None:
        path = self._league_path(league_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_league(self, cfg: dict[str, Any]) -> None:
        self.leagues_dir.mkdir(parents=True, exist_ok=True)
        path = self._league_path(cfg["id"])
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2, sort_keys=False)
            handle.write("\n")
        tmp.replace(path)

    def delete_league(self, league_id: str) -> None:
        path = self._league_path(league_id)
        if path.exists():
            path.unlink()

    # -- draft ------------------------------------------------------------

    def load_picks(self, league_id: str) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM picks WHERE league_id = ? ORDER BY overall",
                (league_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def save_picks(self, league_id: str, picks: list[dict[str, Any]]) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute("DELETE FROM picks WHERE league_id = ?", (league_id,))
                conn.executemany(
                    "INSERT INTO picks (league_id, overall, round, pick_in_round,"
                    " team_id, player_id, player_name, pos, price)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            league_id, p["overall"], p["round"], p["pick_in_round"],
                            p["team_id"], p.get("player_id"), p.get("player_name"),
                            p.get("pos"), p.get("price"),
                        )
                        for p in picks
                    ],
                )
        finally:
            conn.close()

    def clear_picks(self, league_id: str) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute("DELETE FROM picks WHERE league_id = ?", (league_id,))
        finally:
            conn.close()

    # -- rosters ----------------------------------------------------------

    def load_rosters(self, league_id: str) -> dict[str, list[str]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT team_id, player_id FROM rosters WHERE league_id = ?",
                (league_id,),
            ).fetchall()
        finally:
            conn.close()
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["team_id"], []).append(row["player_id"])
        return out

    def set_roster(self, league_id: str, team_id: str, player_ids: list[str]) -> None:
        conn = self._conn()
        try:
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
        finally:
            conn.close()

    # -- trades -----------------------------------------------------------

    def save_trade(self, league_id: str, payload: dict[str, Any], created: str) -> int:
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO saved_trades (league_id, created, payload)"
                    " VALUES (?,?,?)",
                    (league_id, created, json.dumps(payload)),
                )
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def list_trades(self, league_id: str) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, created, payload FROM saved_trades"
                " WHERE league_id = ? ORDER BY id DESC",
                (league_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"id": r["id"], "created": r["created"], **json.loads(r["payload"])}
            for r in rows
        ]

    # -- projections ------------------------------------------------------

    def list_projection_sets(self) -> list[dict[str, Any]]:
        if not self.projections_dir.exists():
            return []
        return [
            {"name": p.name, "size": p.stat().st_size}
            for p in sorted(self.projections_dir.glob("*.csv"))
        ]

    def save_projection_set(self, name: str, csv_text: str) -> None:
        self.projections_dir.mkdir(parents=True, exist_ok=True)
        (self.projections_dir / Path(name).name).write_text(csv_text, encoding="utf-8")

    def load_projection_texts(self) -> list[tuple[str, str]]:
        if not self.projections_dir.exists():
            return []
        return [
            (p.stem, p.read_text(encoding="utf-8"))
            for p in sorted(self.projections_dir.glob("*.csv"))
        ]

    def delete_projection_set(self, name: str) -> bool:
        path = self.projections_dir / Path(name).name
        if not path.exists():
            return False
        path.unlink()
        return True

    # -- tokens -----------------------------------------------------------

    def _token_path(self, platform: str) -> Path:
        return self.secrets_dir / f"{platform}.token.json"

    def save_token(self, platform: str, token: dict[str, Any]) -> None:
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        path = self._token_path(platform)
        path.write_text(json.dumps(token, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def load_token(self, platform: str) -> dict[str, Any] | None:
        path = self._token_path(platform)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    # -- diagnostics ------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "leagues_dir": str(self.leagues_dir),
            "projections_dir": str(self.projections_dir),
            "db_path": str(self.db_path),
        }
