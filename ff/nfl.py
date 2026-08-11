"""Facts about the NFL season itself, as opposed to your league's rules.

Only one fact so far, and it is one no fantasy platform reliably publishes:
**which week each team has off.** That absence is not cosmetic. A player on
bye scores zero, and a projection that does not know it will happily recommend
starting him — the most confidently wrong advice this app could give.

Byes are derived rather than looked up: a team's bye is the regular-season
week in which it appears in no game. That falls straight out of the schedule
and stays correct if the league adds a week, which it has done twice.

The source is nflverse, an open NFL data project on GitHub. Nothing here is
required for the app to run: if the schedule cannot be fetched, byes stay
unknown and every caller carries on, because a missing bye should degrade the
advice rather than break the page.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
from typing import Any

from .players import normalize_team

SCHEDULE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
)
TIMEOUT = 60

#: A season's schedule is a couple of megabytes and never changes once
#: published, so it is fetched at most once per process per season.
_CACHE: dict[int, dict[str, int]] = {}


class ScheduleError(RuntimeError):
    """The schedule could not be fetched or made sense of."""


def _download(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ff-assistant/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        raise ScheduleError(f"Could not fetch the NFL schedule: {exc}") from exc


def parse_bye_weeks(csv_text: str, season: int) -> dict[str, int]:
    """``team`` -> bye week, from a schedule CSV.

    A team playing every week has no bye and is simply absent from the result,
    rather than being given a sentinel that some caller would eventually treat
    as a real week.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    played: dict[str, set[int]] = {}
    weeks: set[int] = set()

    for row in reader:
        if str(row.get("season") or "") != str(season):
            continue
        if (row.get("game_type") or "").upper() != "REG":
            continue
        try:
            week = int(row["week"])
        except (KeyError, TypeError, ValueError):
            continue
        weeks.add(week)
        for side in ("home_team", "away_team"):
            team = normalize_team(row.get(side) or "")
            if team:
                played.setdefault(team, set()).add(week)

    if not weeks:
        return {}

    byes: dict[str, int] = {}
    for team, appearances in played.items():
        missing = sorted(weeks - appearances)
        # Exactly one missing week is a bye. Anything else means the schedule
        # is partial or the team's rows are inconsistent, and guessing which
        # week to zero out would be worse than admitting we do not know.
        if len(missing) == 1:
            byes[team] = missing[0]
    return byes


def bye_weeks(season: int, use_cache: bool = True) -> dict[str, int]:
    """``team`` -> bye week for a season. Empty if the schedule is unreachable."""
    season = int(season)
    if use_cache and season in _CACHE:
        return _CACHE[season]
    byes = parse_bye_weeks(_download(SCHEDULE_URL), season)
    _CACHE[season] = byes
    return byes


def apply_bye_weeks(players: Any, byes: dict[str, int]) -> int:
    """Fill in each player's bye from their team. Returns how many were set.

    A bye already on the player wins: it came from a projection file somebody
    chose to import, and a hand-supplied value should not be overwritten by a
    derived one.
    """
    filled = 0
    for player in players:
        if player.bye or not player.team:
            continue
        week = byes.get(normalize_team(player.team))
        if week:
            player.bye = week
            filled += 1
    return filled


def invalidate() -> None:
    _CACHE.clear()
