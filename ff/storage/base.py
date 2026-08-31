"""The storage contract.

Everything the app persists goes through one of these methods. There are two
implementations:

  * :mod:`ff.storage.files` — JSON files, CSVs and SQLite on local disk. This
    is what ``python3 run.py`` uses, and it keeps the app dependency-free.
  * :mod:`ff.storage.postgres` — a single Postgres database, for hosting where
    the filesystem is read-only and ephemeral.

The split exists because serverless platforms give you no writable disk. Every
write in this app — saving a league setting, making a draft pick, importing
projections, storing a Yahoo token — fails on one. Putting them all behind this
interface means the engine never learns which backend it is running on.
"""

from __future__ import annotations

from typing import Any


class Backend:
    """Persistence operations. Implementations must be safe to construct per
    request: a serverless invocation gets a fresh process with no warm state."""

    name = "base"

    # -- leagues ----------------------------------------------------------

    def list_leagues(self) -> list[dict[str, Any]]:
        """Summary rows: id, name, platform, teams."""
        raise NotImplementedError

    def load_league(self, league_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def save_league(self, cfg: dict[str, Any]) -> None:
        raise NotImplementedError

    def delete_league(self, league_id: str) -> None:
        raise NotImplementedError

    # -- draft ------------------------------------------------------------

    def load_picks(self, league_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def save_picks(self, league_id: str, picks: list[dict[str, Any]]) -> None:
        """Replace every stored pick for this league."""
        raise NotImplementedError

    def clear_picks(self, league_id: str) -> None:
        raise NotImplementedError

    # -- rosters ----------------------------------------------------------

    def load_rosters(self, league_id: str) -> dict[str, list[str]]:
        raise NotImplementedError

    def set_roster(self, league_id: str, team_id: str, player_ids: list[str]) -> None:
        raise NotImplementedError

    # -- trades -----------------------------------------------------------

    def save_trade(self, league_id: str, payload: dict[str, Any], created: str) -> int:
        raise NotImplementedError

    def list_trades(self, league_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    # -- weekly scores ----------------------------------------------------

    def load_scores(self, league_id: str) -> dict[int, dict[str, float]]:
        """Every recorded week: ``{week: {team_id: points}}``."""
        raise NotImplementedError

    def set_week_scores(
        self, league_id: str, week: int, scores: dict[str, float]
    ) -> None:
        """Replace one week's scores. Passing an empty map clears the week."""
        raise NotImplementedError

    # -- transactions -----------------------------------------------------
    #
    # Append-only. Rosters are derived by replaying this log over the draft,
    # so correcting a mistake is a delete, never a roster rebuild.

    def list_transactions(self, league_id: str) -> list[dict[str, Any]]:
        """Oldest first, each carrying the id it was stored under."""
        raise NotImplementedError

    def add_transaction(self, league_id: str, txn: dict[str, Any]) -> int:
        raise NotImplementedError

    def delete_transaction(self, league_id: str, txn_id: int) -> bool:
        raise NotImplementedError

    # -- weekly player status ---------------------------------------------
    #
    # The Sunday-morning path: a player is ruled out at 11:28 and the lineup
    # has to re-solve without re-importing a projection file.

    def load_statuses(self, league_id: str, week: int) -> dict[str, str]:
        raise NotImplementedError

    def set_status(
        self, league_id: str, week: int, player_id: str, status: str
    ) -> None:
        """Store an override, or clear it when ``status`` is empty."""
        raise NotImplementedError

    # -- watchlist --------------------------------------------------------
    #
    # Players you want to be told about the moment they come loose. Most are
    # on somebody else's bench when you add them, which is the point: the
    # pickup you want is decided long before it is available.

    def list_watchlist(self, league_id: str) -> list[dict[str, Any]]:
        """Rows of {player_id, note, added}, oldest first."""
        raise NotImplementedError

    def watch_player(self, league_id: str, player_id: str, note: str, added: str) -> None:
        """Add or update a watch. Re-watching keeps the original ``added``."""
        raise NotImplementedError

    def unwatch_player(self, league_id: str, player_id: str) -> bool:
        raise NotImplementedError

    # -- projections ------------------------------------------------------
    #
    # Stored as the raw CSV text under a name, exactly as uploaded. Parsing
    # happens on load with the same code either way, so a file that imports
    # locally imports identically once hosted.

    def list_projection_sets(self) -> list[dict[str, Any]]:
        """Rows of {name, size}."""
        raise NotImplementedError

    def save_projection_set(self, name: str, csv_text: str) -> None:
        raise NotImplementedError

    def load_projection_texts(self) -> list[tuple[str, str]]:
        """(name, csv_text) for every stored set."""
        raise NotImplementedError

    def delete_projection_set(self, name: str) -> bool:
        raise NotImplementedError

    # -- platform tokens --------------------------------------------------

    def save_token(self, platform: str, token: dict[str, Any]) -> None:
        raise NotImplementedError

    def load_token(self, platform: str) -> dict[str, Any] | None:
        raise NotImplementedError

    # -- diagnostics ------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Shown in the UI so it is always clear where data is going."""
        return {"backend": self.name}
