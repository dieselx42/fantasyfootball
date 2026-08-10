"""The contract every platform adapter implements.

Adding a platform means writing one class here and registering it. Nothing in
the draft board, valuation engine or trade engine knows which platform you use.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..players import Player


class PlatformError(RuntimeError):
    """Raised when a platform cannot serve a request (auth, network, shape)."""


class PlatformAdapter:
    #: Stable key stored in the league config as ``platform.kind``.
    kind: str = "base"
    #: Shown in the setup wizard.
    label: str = "Base"
    description: str = ""
    #: Whether the user must supply credentials before this adapter works.
    requires_auth: bool = False
    #: Fields the setup wizard should collect, as (key, label, help).
    setup_fields: Sequence[tuple[str, str, str]] = ()

    def __init__(self, settings: Mapping[str, Any] | None = None):
        self.settings = dict(settings or {})

    # -- capability probes -------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Whether this adapter is ready to use, and why not if it isn't."""
        return {"kind": self.kind, "ready": True, "detail": ""}

    # -- data ---------------------------------------------------------------

    def fetch_players(self) -> list[Player]:
        """The player universe: names, positions, teams. No projections."""
        raise NotImplementedError

    def fetch_projections(self, season: int) -> list[Player]:
        """Season projections, if the platform exposes them."""
        raise NotImplementedError

    def fetch_adp(self) -> dict[str, float]:
        """Map of ``player_id`` -> average draft position."""
        raise NotImplementedError

    def fetch_league(self, league_id: str) -> dict[str, Any]:
        """League settings, teams and managers, mapped to our config shape."""
        raise NotImplementedError

    def fetch_rosters(self, league_id: str) -> dict[str, list[str]]:
        """Map of ``team_id`` -> list of ``player_id``."""
        raise NotImplementedError
