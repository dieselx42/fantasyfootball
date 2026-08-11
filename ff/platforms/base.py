"""The contract every platform adapter implements.

Adding a platform means writing one class here and registering it. Nothing in
the draft board, valuation engine, waiver engine or trade engine knows which
platform you use.

Two rules make partial adapters first-class, which matters because no two
fantasy sites expose the same things:

  * every fetch method is optional, and the default raises a
    :class:`PlatformError` naming what is missing rather than blowing up with
    ``NotImplementedError``;
  * an adapter declares what it can actually do via :meth:`capabilities`, and
    the UI renders a sync button per declared capability. So an adapter that
    only knows how to read rosters is useful the day it is written, and the
    day it learns to read a scoreboard the button appears on its own.

Return shapes are normalised here, not at the call site. Yahoo, Sleeper and
ESPN disagree about almost everything — team identifiers, stat naming, how a
trade is represented — and the whole point of this layer is that the
disagreement stops at its edge.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..players import Player


class PlatformError(RuntimeError):
    """Raised when a platform cannot serve a request (auth, network, shape)."""


class PlatformUnsupported(PlatformError):
    """Raised when a platform is reachable but simply does not offer this.

    Distinct from :class:`PlatformError` so the UI can say "Sleeper does not
    publish draft results" instead of "something went wrong", which is a very
    different message to act on.
    """


#: Everything an adapter may know how to do. The UI is built from whatever an
#: adapter declares, so adding a capability here plus a method below is all a
#: new platform integration needs.
CAPABILITIES = (
    "league",         # name, season, teams, managers
    "rules",          # scoring, roster slots, waivers, playoffs
    "players",        # the player universe
    "rosters",        # who owns whom right now
    "scoreboard",     # one week's fixtures and points
    "transactions",   # adds, drops, trades
    "draft",          # completed draft results
)

CAPABILITY_LABELS = {
    "league": "Teams and managers",
    "rules": "Scoring and roster rules",
    "players": "Player universe",
    "rosters": "Current rosters",
    "scoreboard": "Weekly scores",
    "transactions": "League transactions",
    "draft": "Draft results",
}


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

    def credential(self, key: str) -> str:
        """A credential for this platform.

        Credentials live in the secret store rather than the league config,
        because league configs are meant to be committed and shared. Values
        passed in directly still win, so the setup wizard can authorise with
        credentials the user has typed but not yet saved.
        """
        if self.settings.get(key):
            return str(self.settings[key])
        from ..config import platform_credentials

        return str(platform_credentials(self.kind).get(key) or "")

    # -- capability probes -------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Whether this adapter is ready to use, and why not if it isn't."""
        return {"kind": self.kind, "ready": True, "detail": ""}

    def capabilities(self) -> set[str]:
        """Which operations this adapter actually implements."""
        return set()

    def describe_capabilities(self) -> list[dict[str, Any]]:
        mine = self.capabilities()
        return [
            {"key": key, "label": CAPABILITY_LABELS[key], "supported": key in mine}
            for key in CAPABILITIES
        ]

    def _unsupported(self, capability: str) -> PlatformUnsupported:
        return PlatformUnsupported(
            f"{self.label} does not provide "
            f"{CAPABILITY_LABELS.get(capability, capability).lower()}."
        )

    # -- data ---------------------------------------------------------------

    def fetch_players(self) -> list[Player]:
        """The player universe: names, positions, teams. No projections."""
        raise self._unsupported("players")

    def fetch_projections(self, season: int) -> list[Player]:
        """Season projections, if the platform exposes them."""
        raise self._unsupported("players")

    def fetch_adp(self) -> dict[str, float]:
        """Map of ``player_id`` -> average draft position."""
        raise self._unsupported("players")

    def fetch_league(self, league_id: str) -> dict[str, Any]:
        """League settings, teams and managers, mapped to our config shape."""
        raise self._unsupported("league")

    def fetch_rosters(self, league_id: str) -> dict[str, list[str]]:
        """Map of ``team_id`` -> list of ``player_id``.

        Player ids must be in *our* form — :func:`ff.players.make_player_id` —
        not the platform's internal keys, or nothing downstream can join them
        to a projection.
        """
        raise self._unsupported("rosters")

    def fetch_rules(self, league_id: str) -> dict[str, Any]:
        """The league's actual rules, in our config's shape.

        Returns any of ``scoring``, ``scoring_bonuses``, ``roster``,
        ``waivers``, ``playoffs``, ``team_count`` — plus, importantly, an
        ``unmapped`` list naming every platform rule this adapter could not
        translate. Silently dropping a scoring rule would change every
        valuation in the app with nothing on screen to explain why, so the
        gaps are part of the return value rather than a log line.
        """
        raise self._unsupported("rules")

    def fetch_scoreboard(self, league_id: str, week: int) -> dict[str, Any]:
        """One week of results.

        ``{"week": int,
           "scores": {team_id: points},
           "games": [{"home": team_id, "away": team_id}],
           "final": bool}``
        """
        raise self._unsupported("scoreboard")

    def fetch_transactions(self, league_id: str) -> list[dict[str, Any]]:
        """Roster moves, shaped for :class:`ff.transactions.Transaction`.

        ``adds`` and ``drops`` are lists of ``{"name", "pos", "team"}`` rather
        than of ids. Our player ids are derived from name and position, so
        handing over all three lets the sync layer rebuild an exact id — and
        an adapter never has to learn this app's id scheme.
        """
        raise self._unsupported("transactions")

    def fetch_draft_results(self, league_id: str) -> list[dict[str, Any]]:
        """Completed draft picks: ``{round, pick, team_id, name, pos, price}``."""
        raise self._unsupported("draft")
