"""Postgres backend, for hosting where the filesystem is read-only.

Selected automatically when ``DATABASE_URL`` is set. Everything the file
backend keeps on disk lives in five tables here instead.

The important design choice: a league's rules stay a single JSON document in
``leagues.config``. That is the exact dict ``ff.config`` already builds and
validates, so the schema, defaults, validation and ``migrate()`` all work
unchanged. Adding a scoring rule stays a config change, never a migration.

``owner_id`` columns are present but unused. They are here so that adding
sign-in later is an additive change — a backfill and a WHERE clause — rather
than a table rewrite.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from .base import Backend

SCHEMA = """
CREATE TABLE IF NOT EXISTS leagues (
    id         TEXT PRIMARY KEY,
    owner_id   TEXT,
    name       TEXT NOT NULL,
    season     INTEGER,
    config     JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS picks (
    league_id     TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    overall       INTEGER NOT NULL,
    round         INTEGER NOT NULL,
    pick_in_round INTEGER NOT NULL,
    team_id       TEXT NOT NULL,
    player_id     TEXT,
    player_name   TEXT,
    pos           TEXT,
    price         DOUBLE PRECISION,
    PRIMARY KEY (league_id, overall)
);

CREATE TABLE IF NOT EXISTS rosters (
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    team_id   TEXT NOT NULL,
    player_id TEXT NOT NULL,
    acquired  TEXT NOT NULL DEFAULT 'draft',
    PRIMARY KEY (league_id, team_id, player_id)
);

CREATE TABLE IF NOT EXISTS saved_trades (
    id        BIGSERIAL PRIMARY KEY,
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    created   TEXT NOT NULL,
    payload   JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS projection_sets (
    name        TEXT PRIMARY KEY,
    owner_id    TEXT,
    csv_text    TEXT NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform_tokens (
    platform   TEXT NOT NULL,
    owner_id   TEXT NOT NULL DEFAULT '',
    token      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, owner_id)
);

CREATE TABLE IF NOT EXISTS week_scores (
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    week      INTEGER NOT NULL,
    team_id   TEXT NOT NULL,
    points    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (league_id, week, team_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id              BIGSERIAL PRIMARY KEY,
    league_id       TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    team_id         TEXT NOT NULL,
    partner_team_id TEXT,
    week            INTEGER,
    date            TEXT,
    adds            JSONB NOT NULL DEFAULT '[]'::jsonb,
    drops           JSONB NOT NULL DEFAULT '[]'::jsonb,
    faab            DOUBLE PRECISION,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS transactions_league_idx ON transactions (league_id, id);

CREATE TABLE IF NOT EXISTS player_status (
    league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    week      INTEGER NOT NULL,
    player_id TEXT NOT NULL,
    status    TEXT NOT NULL,
    PRIMARY KEY (league_id, week, player_id)
);
"""

_schema_lock = threading.Lock()
_schema_ready = False


class PostgresBackend(Backend):
    name = "postgres"

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ.get("DATABASE_URL") or ""
        if not self.dsn:
            raise RuntimeError("DATABASE_URL is not set.")

    # -- connection -------------------------------------------------------

    def _connect(self):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "psycopg2 is required for the Postgres backend. "
                "Add psycopg2-binary to requirements.txt."
            ) from exc

        conn = psycopg2.connect(self.dsn, connect_timeout=10)
        conn.autocommit = False
        return conn

    def _ensure_schema(self, conn) -> None:
        """Create tables on first use so deploying needs no migration step."""
        global _schema_ready
        if _schema_ready:
            return
        with _schema_lock:
            if _schema_ready:
                return
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
            conn.commit()
            _schema_ready = True

    def _cursor(self, conn):
        import psycopg2.extras

        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def _run(self, fn):
        """Open a connection, ensure the schema, run ``fn(conn)``, commit."""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            result = fn(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- leagues ----------------------------------------------------------

    def list_leagues(self) -> list[dict[str, Any]]:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute("SELECT id, config FROM leagues ORDER BY name")
                rows = cur.fetchall()
            out = []
            for row in rows:
                cfg = row["config"]
                out.append(
                    {
                        "id": row["id"],
                        "name": cfg.get("name", row["id"]),
                        "platform": (cfg.get("platform") or {}).get("kind", "manual"),
                        "teams": str(len(cfg.get("teams") or [])),
                    }
                )
            return out

        return self._run(op)

    def load_league(self, league_id: str) -> dict[str, Any] | None:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute("SELECT config FROM leagues WHERE id = %s", (league_id,))
                row = cur.fetchone()
            return row["config"] if row else None

        return self._run(op)

    def save_league(self, cfg: dict[str, Any]) -> None:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO leagues (id, name, season, config)"
                    " VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT (id) DO UPDATE SET"
                    "   name = EXCLUDED.name,"
                    "   season = EXCLUDED.season,"
                    "   config = EXCLUDED.config,"
                    "   updated_at = now()",
                    (
                        cfg["id"],
                        cfg.get("name", cfg["id"]),
                        cfg.get("season"),
                        json.dumps(cfg),
                    ),
                )

        self._run(op)

    def delete_league(self, league_id: str) -> None:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM leagues WHERE id = %s", (league_id,))

        self._run(op)

    # -- draft ------------------------------------------------------------

    def load_picks(self, league_id: str) -> list[dict[str, Any]]:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT overall, round, pick_in_round, team_id, player_id,"
                    " player_name, pos, price FROM picks"
                    " WHERE league_id = %s ORDER BY overall",
                    (league_id,),
                )
                return [dict(r) for r in cur.fetchall()]

        return self._run(op)

    def save_picks(self, league_id: str, picks: list[dict[str, Any]]) -> None:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM picks WHERE league_id = %s", (league_id,))
                if picks:
                    cur.executemany(
                        "INSERT INTO picks (league_id, overall, round, pick_in_round,"
                        " team_id, player_id, player_name, pos, price)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        [
                            (
                                league_id, p["overall"], p["round"],
                                p["pick_in_round"], p["team_id"], p.get("player_id"),
                                p.get("player_name"), p.get("pos"), p.get("price"),
                            )
                            for p in picks
                        ],
                    )

        self._run(op)

    def clear_picks(self, league_id: str) -> None:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM picks WHERE league_id = %s", (league_id,))

        self._run(op)

    # -- rosters ----------------------------------------------------------

    def load_rosters(self, league_id: str) -> dict[str, list[str]]:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT team_id, player_id FROM rosters WHERE league_id = %s",
                    (league_id,),
                )
                rows = cur.fetchall()
            out: dict[str, list[str]] = {}
            for row in rows:
                out.setdefault(row["team_id"], []).append(row["player_id"])
            return out

        return self._run(op)

    def set_roster(self, league_id: str, team_id: str, player_ids: list[str]) -> None:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM rosters WHERE league_id = %s AND team_id = %s",
                    (league_id, team_id),
                )
                if player_ids:
                    cur.executemany(
                        "INSERT INTO rosters (league_id, team_id, player_id)"
                        " VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        [(league_id, team_id, pid) for pid in player_ids],
                    )

        self._run(op)

    # -- trades -----------------------------------------------------------

    def save_trade(self, league_id: str, payload: dict[str, Any], created: str) -> int:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO saved_trades (league_id, created, payload)"
                    " VALUES (%s,%s,%s) RETURNING id",
                    (league_id, created, json.dumps(payload)),
                )
                return int(cur.fetchone()[0])

        return self._run(op)

    def list_trades(self, league_id: str) -> list[dict[str, Any]]:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT id, created, payload FROM saved_trades"
                    " WHERE league_id = %s ORDER BY id DESC",
                    (league_id,),
                )
                return [
                    {"id": r["id"], "created": r["created"], **r["payload"]}
                    for r in cur.fetchall()
                ]

        return self._run(op)

    # -- weekly scores ----------------------------------------------------

    def load_scores(self, league_id: str) -> dict[int, dict[str, float]]:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT week, team_id, points FROM week_scores"
                    " WHERE league_id = %s ORDER BY week",
                    (league_id,),
                )
                rows = cur.fetchall()
            out: dict[int, dict[str, float]] = {}
            for row in rows:
                out.setdefault(int(row["week"]), {})[row["team_id"]] = float(
                    row["points"]
                )
            return out

        return self._run(op)

    def set_week_scores(
        self, league_id: str, week: int, scores: dict[str, float]
    ) -> None:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM week_scores WHERE league_id = %s AND week = %s",
                    (league_id, int(week)),
                )
                if scores:
                    cur.executemany(
                        "INSERT INTO week_scores (league_id, week, team_id, points)"
                        " VALUES (%s,%s,%s,%s)",
                        [
                            (league_id, int(week), team_id, float(points))
                            for team_id, points in scores.items()
                        ],
                    )

        self._run(op)

    # -- transactions -----------------------------------------------------

    def list_transactions(self, league_id: str) -> list[dict[str, Any]]:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT id, type, team_id, partner_team_id, week, date,"
                    " adds, drops, faab, note FROM transactions"
                    " WHERE league_id = %s ORDER BY id",
                    (league_id,),
                )
                return [dict(r) for r in cur.fetchall()]

        return self._run(op)

    def add_transaction(self, league_id: str, txn: dict[str, Any]) -> int:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO transactions (league_id, type, team_id,"
                    " partner_team_id, week, date, adds, drops, faab, note)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (
                        league_id,
                        txn.get("type", "add"),
                        txn.get("team_id", ""),
                        txn.get("partner_team_id"),
                        txn.get("week"),
                        txn.get("date", ""),
                        json.dumps(list(txn.get("adds") or [])),
                        json.dumps(list(txn.get("drops") or [])),
                        txn.get("faab"),
                        txn.get("note", ""),
                    ),
                )
                return int(cur.fetchone()[0])

        return self._run(op)

    def delete_transaction(self, league_id: str, txn_id: int) -> bool:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM transactions WHERE league_id = %s AND id = %s",
                    (league_id, int(txn_id)),
                )
                return cur.rowcount > 0

        return self._run(op)

    # -- weekly player status ---------------------------------------------

    def load_statuses(self, league_id: str, week: int) -> dict[str, str]:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT player_id, status FROM player_status"
                    " WHERE league_id = %s AND week = %s",
                    (league_id, int(week)),
                )
                return {r["player_id"]: r["status"] for r in cur.fetchall()}

        return self._run(op)

    def set_status(
        self, league_id: str, week: int, player_id: str, status: str
    ) -> None:
        def op(conn):
            with conn.cursor() as cur:
                if not status:
                    cur.execute(
                        "DELETE FROM player_status"
                        " WHERE league_id = %s AND week = %s AND player_id = %s",
                        (league_id, int(week), player_id),
                    )
                else:
                    cur.execute(
                        "INSERT INTO player_status (league_id, week, player_id, status)"
                        " VALUES (%s,%s,%s,%s)"
                        " ON CONFLICT (league_id, week, player_id) DO UPDATE SET"
                        "   status = EXCLUDED.status",
                        (league_id, int(week), player_id, status),
                    )

        self._run(op)

    # -- projections ------------------------------------------------------

    def list_projection_sets(self) -> list[dict[str, Any]]:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT name, length(csv_text) AS size FROM projection_sets"
                    " ORDER BY name"
                )
                return [{"name": r["name"], "size": r["size"]} for r in cur.fetchall()]

        return self._run(op)

    def save_projection_set(self, name: str, csv_text: str) -> None:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO projection_sets (name, csv_text) VALUES (%s,%s)"
                    " ON CONFLICT (name) DO UPDATE SET"
                    "   csv_text = EXCLUDED.csv_text, uploaded_at = now()",
                    (name, csv_text),
                )

        self._run(op)

    def load_projection_texts(self) -> list[tuple[str, str]]:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute("SELECT name, csv_text FROM projection_sets ORDER BY name")
                return [(r["name"], r["csv_text"]) for r in cur.fetchall()]

        return self._run(op)

    def delete_projection_set(self, name: str) -> bool:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM projection_sets WHERE name = %s", (name,))
                return cur.rowcount > 0

        return self._run(op)

    # -- tokens -----------------------------------------------------------

    def save_token(self, platform: str, token: dict[str, Any]) -> None:
        def op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO platform_tokens (platform, owner_id, token)"
                    " VALUES (%s, '', %s)"
                    " ON CONFLICT (platform, owner_id) DO UPDATE SET"
                    "   token = EXCLUDED.token, updated_at = now()",
                    (platform, json.dumps(token)),
                )

        self._run(op)

    def load_token(self, platform: str) -> dict[str, Any] | None:
        def op(conn):
            with self._cursor(conn) as cur:
                cur.execute(
                    "SELECT token FROM platform_tokens"
                    " WHERE platform = %s AND owner_id = ''",
                    (platform,),
                )
                row = cur.fetchone()
            return row["token"] if row else None

        return self._run(op)

    # -- diagnostics ------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        # Never echo the DSN; it carries the password.
        host = ""
        if "@" in self.dsn:
            host = self.dsn.rsplit("@", 1)[-1].split("/")[0]
        return {"backend": self.name, "host": host}
