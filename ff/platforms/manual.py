"""Manual adapter — no platform integration at all.

This is the fallback that always works: you type the league in, import a
projections CSV, and click players off the board as they go. Every feature in
the app works in manual mode; platform adapters only save typing.
"""

from __future__ import annotations

from typing import Any

from ..players import Player, load_projections
from .base import PlatformAdapter


class ManualAdapter(PlatformAdapter):
    kind = "manual"
    label = "Manual / other"
    description = "No API. Enter the league yourself and import a projections CSV."
    requires_auth = False
    setup_fields = ()

    def capabilities(self) -> set[str]:
        # Nothing is synced because nothing is connected. Every one of these
        # is still available in the app — you enter it rather than fetch it.
        return {"players"}

    def status(self) -> dict[str, Any]:
        return {"kind": self.kind, "ready": True, "detail": "No connection needed."}

    def fetch_players(self) -> list[Player]:
        return load_projections()

    def fetch_adp(self) -> dict[str, float]:
        return {p.player_id: p.adp for p in load_projections() if p.adp}
