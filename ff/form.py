"""Blend a preseason projection with what a player has actually done.

A season-long projection is the best guess available in August and the worst
one available in November. It cannot know that a rookie tight end has caught
nothing in three weeks, or that a receiver has taken over a passing game. But
two games of real production is not a projection either — one touchdown makes
a journeyman look like a star, and betting a lineup on it is how people chase
last week's points.

So blend, with the weight on form growing as evidence accumulates::

    weight = games_played / (games_played + PRIOR_GAMES)

``PRIOR_GAMES`` is how many games of real production it takes to matter as
much as the preseason number. At 4, two games buys form a third of the say and
six games buys it 60% — roughly how a careful manager updates. It is a dial,
not a discovery: raise it to trust the projection longer, lower it to react
faster.

Nothing here knows about matchups. A projection that cannot see that a defence
is the league's worst is missing something real, and this does not fix that.
What it fixes is a number frozen in August.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from .players import Player

#: Games of real production that carry the same weight as the preseason
#: projection. Four is deliberately conservative — fantasy weeks are noisy
#: enough that reacting faster mostly chases variance.
PRIOR_GAMES = 4.0


def form_weight(games_played: int, prior_games: float = PRIOR_GAMES) -> float:
    """How much of the blend comes from actual production, 0 to 1."""
    if games_played <= 0:
        return 0.0
    return games_played / (games_played + max(prior_games, 0.0001))


def per_game_projection(
    player: Player,
    actuals: Sequence[float],
    season_games: float = 17.0,
    prior_games: float = PRIOR_GAMES,
    replacement: float = 0.0,
) -> float:
    """One week's expectation, preseason pace updated by games played.

    ``actuals`` is this player's real score in each week he has played. Weeks
    he missed are not passed in: a projection answers "what if he plays", and
    availability is a separate question the lineup already asks through byes
    and injury status.

    ``replacement`` is the per-week prior for a player the preseason file
    never listed. It matters more than it looks: anyone who broke out after
    the file was built has no projection, and treating that as a *zero* prior
    buries him under players the file happened to include. A kicker with 4 and
    14 on the board came out at 3.00 that way. Replacement level is what an
    unlisted player is actually worth before you have seen him play, so that
    is what the blend should regress him towards.
    """
    pace = player.points / season_games if season_games else 0.0
    if pace <= 0:
        pace = max(replacement, 0.0)
    if not actuals:
        return round(pace, 2)
    form = sum(actuals) / len(actuals)
    w = form_weight(len(actuals), prior_games)
    return round(pace * (1 - w) + form * w, 2)


def project_roster(
    players: Iterable[Player],
    actuals: Mapping[str, Sequence[float]],
    season_games: float = 17.0,
    prior_games: float = PRIOR_GAMES,
    replacement: Mapping[str, float] | None = None,
) -> list[Player]:
    """Copies of ``players`` with ``points`` set to this week's expectation.

    ``replacement`` maps position to a *season* replacement level, the same
    shape ``valuation.replacement_levels`` returns; it is divided down here so
    callers do not have to remember which scale it is in.

    The season total moves to ``season_points`` so nothing downstream loses
    it — the trade engine and draft board both still want the full-season
    number, and only start/sit wants the weekly one.
    """
    levels = replacement or {}
    out = []
    for player in players:
        floor = levels.get(player.pos, 0.0) / season_games if season_games else 0.0
        weekly = per_game_projection(
            player, actuals.get(player.player_id, ()), season_games, prior_games, floor
        )
        clone = Player(**{**player.__dict__})
        clone.season_points = player.points
        clone.points = weekly
        out.append(clone)
    return out
