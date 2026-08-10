"""Head-to-head schedule, weekly results, and the standings they produce.

Two jobs, and they run in opposite directions. Before the games, this module
projects every team's best lineup so you know which matchups are close and how
much you need from a flex decision. After them, it takes the scores that
actually happened and answers the only question anyone asks on Monday: did we
win, and where does that leave us?

The schedule can come from the league config — paste in the real one your
platform generated and every result lines up with the site. When it is absent
a round-robin is generated with the circle method, which is deterministic, so
the same league always produces the same fixtures.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .players import Player
from .roster import optimal_lineup
from .weekly import regular_season_weeks, season_weeks


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------

def generate_schedule(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A round-robin over the regular season, by the circle method.

    One team is held fixed and the rest rotate, which pairs everyone exactly
    once every ``teams - 1`` weeks and then repeats — the same thing real
    leagues do when the season is longer than a single round robin. With an
    odd number of teams one fixture per week is a bye.
    """
    team_ids = [t["id"] for t in cfg.get("teams") or []]
    weeks = regular_season_weeks(cfg)
    if len(team_ids) < 2:
        return [{"week": week, "games": []} for week in weeks]

    rotation: list[str | None] = list(team_ids)
    if len(rotation) % 2:
        rotation.append(None)               # odd league: someone sits each week
    size = len(rotation)

    out: list[dict[str, Any]] = []
    for index, week in enumerate(weeks):
        games = []
        for slot in range(size // 2):
            home, away = rotation[slot], rotation[size - 1 - slot]
            if home is None or away is None:
                continue
            # Flip the fixed pairing every other round so the same two teams
            # do not always meet with the same home side.
            if index % 2 and slot == 0:
                home, away = away, home
            games.append({"home": home, "away": away})
        out.append({"week": week, "games": games})
        rotation = [rotation[0], rotation[-1], *rotation[1:-1]]

    return out


def schedule(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The league's fixtures: the configured schedule, or a generated one."""
    explicit = cfg.get("schedule") or []
    if not explicit:
        return generate_schedule(cfg)

    known = {t["id"] for t in cfg.get("teams") or []}
    out = []
    for entry in explicit:
        try:
            week = int(entry.get("week"))
        except (TypeError, ValueError):
            continue
        games = [
            {"home": g.get("home"), "away": g.get("away")}
            for g in entry.get("games") or []
            if g.get("home") in known and g.get("away") in known
        ]
        out.append({"week": week, "games": games})
    return sorted(out, key=lambda e: e["week"])


def default_week(
    cfg: Mapping[str, Any], scores: Mapping[int, Mapping[str, float]]
) -> int:
    """The week the app should open on: the one after the last one scored."""
    weeks = season_weeks(cfg)
    if not weeks:
        return 1
    played = sorted(int(w) for w in scores if scores.get(w))
    if not played:
        return weeks[0]
    following = played[-1] + 1
    return following if following in weeks else weeks[-1]


def week_games(cfg: Mapping[str, Any], week: int) -> list[dict[str, Any]]:
    for entry in schedule(cfg):
        if int(entry["week"]) == int(week):
            return entry["games"]
    return []


def opponent_of(cfg: Mapping[str, Any], team_id: str, week: int) -> str | None:
    for game in week_games(cfg, week):
        if game["home"] == team_id:
            return game["away"]
        if game["away"] == team_id:
            return game["home"]
    return None


# --------------------------------------------------------------------------
# Projections, before the games
# --------------------------------------------------------------------------

def project_week(
    cfg: Mapping[str, Any],
    week: int,
    rosters: Mapping[str, Sequence[Player]],
) -> dict[str, float]:
    """Each team's best legal lineup for ``week``, in points.

    Rosters must already be projected for the week — see :mod:`ff.weekly`.
    """
    return {
        team_id: optimal_lineup(players, cfg)[1]
        for team_id, players in rosters.items()
    }


# --------------------------------------------------------------------------
# Results, after them
# --------------------------------------------------------------------------

def _decide(home_pts: float | None, away_pts: float | None) -> str:
    if home_pts is None or away_pts is None:
        return "pending"
    if home_pts > away_pts:
        return "home"
    if away_pts > home_pts:
        return "away"
    return "tie"


def week_summary(
    cfg: Mapping[str, Any],
    week: int,
    scores: Mapping[int, Mapping[str, float]],
    projected: Mapping[str, float] | None = None,
    my_team_id: str | None = None,
) -> dict[str, Any]:
    """Every game in a week, scored where scores exist and projected where not."""
    actual = {str(k): float(v) for k, v in (scores.get(int(week)) or {}).items()}
    projected = {str(k): float(v) for k, v in (projected or {}).items()}
    me = my_team_id or cfg.get("my_team_id")

    games = []
    for game in week_games(cfg, week):
        home, away = game["home"], game["away"]
        home_pts = actual.get(home)
        away_pts = actual.get(away)
        row = {
            "home": home,
            "away": away,
            "home_points": home_pts,
            "away_points": away_pts,
            "home_projected": projected.get(home),
            "away_projected": projected.get(away),
            "outcome": _decide(home_pts, away_pts),
            "final": home_pts is not None and away_pts is not None,
        }
        row["margin"] = (
            round(abs(home_pts - away_pts), 2) if row["final"] else None
        )
        games.append(row)

    my_game = None
    for row in games:
        if me not in (row["home"], row["away"]):
            continue
        mine = "home" if row["home"] == me else "away"
        theirs = "away" if mine == "home" else "home"
        my_game = {
            **row,
            "opponent": row[theirs],
            "my_points": row[f"{mine}_points"],
            "opponent_points": row[f"{theirs}_points"],
            "my_projected": row[f"{mine}_projected"],
            "opponent_projected": row[f"{theirs}_projected"],
            "result": (
                "pending" if not row["final"]
                else "win" if row["outcome"] == mine
                else "tie" if row["outcome"] == "tie"
                else "loss"
            ),
        }
        break

    return {
        "week": int(week),
        "games": games,
        "my_game": my_game,
        "complete": bool(games) and all(g["final"] for g in games),
        "median": median_score(actual) if actual else None,
    }


def median_score(week_scores: Mapping[str, float]) -> float | None:
    values = sorted(float(v) for v in week_scores.values())
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return round(values[middle], 2)
    return round((values[middle - 1] + values[middle]) / 2, 2)


def standings(
    cfg: Mapping[str, Any], scores: Mapping[int, Mapping[str, float]]
) -> list[dict[str, Any]]:
    """Win-loss records, points for and against, and the playoff line.

    Honours ``settings_misc.play_against_median``: leagues that use it give
    every team a second weekly result against the league median, which halves
    the influence of a lucky schedule.
    """
    teams = [t["id"] for t in cfg.get("teams") or []]
    misc = cfg.get("settings_misc") or {}
    use_median = bool(misc.get("play_against_median"))

    table = {
        team_id: {
            "team_id": team_id,
            "wins": 0, "losses": 0, "ties": 0,
            "points_for": 0.0, "points_against": 0.0,
            "median_wins": 0, "median_losses": 0,
            "results": [],
        }
        for team_id in teams
    }

    for entry in schedule(cfg):
        week = int(entry["week"])
        week_scores = {
            str(k): float(v) for k, v in (scores.get(week) or {}).items()
        }
        if not week_scores:
            continue

        for game in entry["games"]:
            home, away = game["home"], game["away"]
            if home not in week_scores or away not in week_scores:
                continue
            if home not in table or away not in table:
                continue
            home_pts, away_pts = week_scores[home], week_scores[away]
            table[home]["points_for"] += home_pts
            table[home]["points_against"] += away_pts
            table[away]["points_for"] += away_pts
            table[away]["points_against"] += home_pts

            if home_pts > away_pts:
                _record(table[home], "W")
                _record(table[away], "L")
            elif away_pts > home_pts:
                _record(table[away], "W")
                _record(table[home], "L")
            else:
                _record(table[home], "T")
                _record(table[away], "T")

        if use_median:
            middle = median_score(week_scores)
            for team_id, points in week_scores.items():
                if team_id not in table or middle is None:
                    continue
                if points > middle:
                    table[team_id]["median_wins"] += 1
                elif points < middle:
                    table[team_id]["median_losses"] += 1

    rows = []
    for team_id in teams:
        row = table[team_id]
        wins = row["wins"] + (row["median_wins"] if use_median else 0)
        losses = row["losses"] + (row["median_losses"] if use_median else 0)
        played = wins + losses + row["ties"]
        rows.append(
            {
                **row,
                "points_for": round(row["points_for"], 2),
                "points_against": round(row["points_against"], 2),
                "total_wins": wins,
                "total_losses": losses,
                "games_played": played,
                "win_pct": round((wins + 0.5 * row["ties"]) / played, 3) if played else 0.0,
                "streak": _streak(row["results"]),
                "record": f"{wins}-{losses}" + (f"-{row['ties']}" if row["ties"] else ""),
            }
        )

    # Yahoo breaks ties on head-to-head first and points-for second. Points-for
    # is the one that holds for any number of tied teams, so it is used here
    # and the tiebreaker in force is reported rather than assumed.
    rows.sort(key=lambda r: (r["win_pct"], r["points_for"]), reverse=True)
    playoff_spots = int((cfg.get("playoffs") or {}).get("teams") or 0)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
        row["in_playoffs"] = bool(playoff_spots) and rank <= playoff_spots

    return rows


def _record(row: dict[str, Any], outcome: str) -> None:
    key = {"W": "wins", "L": "losses", "T": "ties"}[outcome]
    row[key] += 1
    row["results"].append(outcome)


def _streak(results: Sequence[str]) -> str:
    if not results:
        return "—"
    last = results[-1]
    run = 0
    for outcome in reversed(results):
        if outcome != last:
            break
        run += 1
    return f"{last}{run}"


def season_summary(
    cfg: Mapping[str, Any], scores: Mapping[int, Mapping[str, float]]
) -> dict[str, Any]:
    """Standings plus the week-by-week context around your own team."""
    rows = standings(cfg, scores)
    me = cfg.get("my_team_id")
    mine = next((r for r in rows if r["team_id"] == me), None)
    played = sorted(int(w) for w in scores if scores.get(w))
    return {
        "standings": rows,
        "my_team_id": me,
        "my_record": mine,
        "weeks_played": played,
        "last_week": played[-1] if played else None,
        "playoff_teams": int((cfg.get("playoffs") or {}).get("teams") or 0),
        "playoff_weeks": [int(w) for w in (cfg.get("playoffs") or {}).get("weeks") or []],
        "tiebreaker": "points for",
    }
