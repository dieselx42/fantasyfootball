"""Pulling a platform's state into this app's.

Adapters answer in a normalised shape but still in *their* vocabulary: their
team identifiers, and players named rather than keyed. This module is where
that meets our league config and our player pool, and it exists as its own
layer for one reason — the joins can fail, and how they fail matters.

Two joins are load-bearing:

**Teams.** A platform's team id ("1", "nfl.l.184206.t.7", a roster id) is not
our config's team id ("diesel"). They are matched by platform key first, then
by name, and anything left over is *reported*. A silently unmatched team means
a scoreboard that quietly drops a fixture, or transactions attributed to
nobody.

**Players.** Adapters hand over name, position and team. Our ids are derived
directly from name plus position, so most reconstruct exactly; the rest are
matched by normalised name against the loaded pool. A player who matches
nothing is listed by name in the result rather than dropped, because "we
imported 14 of your 17 moves" is only useful if you can see which three are
missing.

Nothing here writes silently. Every function returns a report of what changed
and what it could not place.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from . import store
from .config import slugify
from .players import Player, make_player_id, normalize_name, normalize_pos
from .platforms import PlatformError, adapter_for
from .transactions import Transaction


# --------------------------------------------------------------------------
# Joining a platform's identifiers to ours
# --------------------------------------------------------------------------

def team_map(cfg: Mapping[str, Any]) -> dict[str, str]:
    """Platform team id -> our team id.

    Config teams may carry a ``platform_key`` from an earlier league import;
    that is the reliable join. Names are the fallback, because a league that
    was typed in by hand has no keys at all.
    """
    out: dict[str, str] = {}
    for team in cfg.get("teams") or []:
        our_id = team.get("id")
        if not our_id:
            continue
        key = team.get("platform_key")
        if key:
            out[str(key)] = our_id
            out[str(key).rsplit(".", 1)[-1]] = our_id
        if team.get("platform_team_id"):
            out[str(team["platform_team_id"])] = our_id
        # A hand-entered league often slugifies to the same id the platform
        # would produce from the same name.
        out.setdefault(slugify(team.get("name", "")), our_id)
        out.setdefault(our_id, our_id)
    return out


def resolve_team(mapping: Mapping[str, str], platform_id: Any) -> str | None:
    if platform_id in (None, ""):
        return None
    key = str(platform_id)
    return mapping.get(key) or mapping.get(key.rsplit(".", 1)[-1])


def has_platform_keys(cfg: Mapping[str, Any]) -> bool:
    return any(t.get("platform_key") for t in cfg.get("teams") or [])


def _team_hint(cfg: Mapping[str, Any], unmatched: Sequence[str]) -> str:
    """Why teams failed to match, in terms of what to do about it.

    Platform team ids ("1", "nfl.l.184206.t.7") mean nothing to a league that
    was typed in by hand. Importing teams first stores each one's platform key,
    and every later sync joins on that. Without this hint the failure reads as
    a bug rather than an ordering problem.
    """
    if not unmatched:
        return ""
    if not has_platform_keys(cfg):
        return (
            "None of your teams carry a platform key yet, so there is nothing "
            "to match these ids against. Sync 'Teams and managers' first — it "
            "records the keys, and every other sync joins on them."
        )
    return (
        "These platform teams matched nothing in your league. If you renamed "
        "teams here after importing them, re-sync 'Teams and managers'."
    )


def player_index(pool: Iterable[Player]) -> dict[str, str]:
    """Lookup keys -> our player id.

    Three keys per player: the id itself (so an adapter that already speaks
    our ids just works), name plus position, and name alone. Name-plus-
    position is tried first, because two players do share a name and it is
    almost never at the same position.
    """
    index: dict[str, str] = {}
    for player in pool:
        name = normalize_name(player.name)
        index.setdefault(player.player_id, player.player_id)
        index.setdefault(f"{name}|{player.pos}", player.player_id)
        index.setdefault(name, player.player_id)
    return index


def resolve_player(
    index: Mapping[str, str], reference: Mapping[str, Any] | str
) -> str | None:
    """Our player id for an adapter's player reference.

    Adapters send ``{"name", "pos", "team"}``; a bare string is accepted too so
    a minimal adapter can send just a name.
    """
    if isinstance(reference, str):
        name, pos = reference, ""
    else:
        name = str(reference.get("name") or "")
        pos = normalize_pos(str(reference.get("pos") or ""))
    if not name:
        return None

    normalized = normalize_name(name)
    if pos:
        # Our ids are name-plus-position, so this is an exact hit whenever the
        # pool holds the player at all.
        found = index.get(make_player_id(name, pos, "")) or index.get(f"{normalized}|{pos}")
        if found:
            return found
    return index.get(normalized)


# --------------------------------------------------------------------------
# The sync operations
# --------------------------------------------------------------------------

def _league_key(cfg: Mapping[str, Any]) -> str:
    platform = cfg.get("platform") or {}
    key = (platform.get("settings") or {}).get("league_id") or platform.get("league_id")
    if not key:
        raise PlatformError("Set your platform league ID first.")
    return str(key)


def sync_rules(cfg: dict[str, Any]) -> dict[str, Any]:
    """Read the league's real scoring and roster rules off the platform.

    Returns the *proposed* config changes rather than applying them. Scoring
    drives every valuation in the app, so overwriting it is a decision worth
    showing someone before it happens.
    """
    adapter = adapter_for(cfg)
    rules = adapter.fetch_rules(_league_key(cfg))
    unmapped = rules.pop("unmapped", [])

    changes = []
    for block, value in rules.items():
        before = cfg.get(block)
        if before != value:
            changes.append({"block": block, "before": before, "after": value})

    return {
        "proposed": rules,
        "changes": changes,
        "unmapped": unmapped,
        "applied": False,
    }


#: Blocks a rules sync is allowed to write. ``team_count`` is deliberately
#: absent: the teams sync owns it, and writing it here can leave a config that
#: claims ten teams while listing two — which then fails validation and blocks
#: the scoring import that was the actual point of the operation.
APPLICABLE_RULES = ("scoring", "scoring_bands", "roster", "playoffs", "waivers")


def apply_rules(cfg: dict[str, Any], proposed: Mapping[str, Any]) -> list[str]:
    """Merge accepted rule changes into the config. Returns the blocks written."""
    written = []
    for block in APPLICABLE_RULES:
        if block in proposed:
            cfg[block] = proposed[block]
            written.append(block)
    return written


def sync_league(cfg: dict[str, Any]) -> dict[str, Any]:
    """Teams and managers, keeping each team's platform key for later joins."""
    adapter = adapter_for(cfg)
    remote = adapter.fetch_league(_league_key(cfg))
    teams = remote.get("teams") or []
    if not teams:
        return {"teams": 0, "detail": "The platform returned no teams."}

    existing = {t.get("id"): t for t in cfg.get("teams") or []}
    merged = []
    for team in teams:
        our_id = slugify(team.get("name") or team.get("id") or "team")
        previous = existing.get(our_id, {})
        merged.append(
            {
                "id": our_id,
                "name": team.get("name", "Team"),
                "manager": team.get("manager", "") or previous.get("manager", ""),
                # Kept so every later sync can join this platform's ids to
                # ours without falling back to name matching.
                "platform_key": team.get("platform_key") or team.get("id"),
            }
        )

    before = len(existing)
    cfg["teams"] = merged
    cfg["team_count"] = len(merged)
    if remote.get("name"):
        cfg["name"] = remote["name"]
    if remote.get("season"):
        cfg["season"] = remote["season"]

    result: dict[str, Any] = {"teams": len(merged), "replaced": before}
    if before and len(merged) < before:
        # Team ids key rosters, scores and the transaction log, so losing one
        # orphans its history. That is worth saying out loud rather than
        # leaving to be discovered when the standings come up short.
        dropped = sorted(set(existing) - {t["id"] for t in merged})
        result["dropped_teams"] = dropped
        result["hint"] = (
            f"Your league went from {before} teams to {len(merged)}. Any "
            f"rosters, scores or transactions stored against {', '.join(dropped)} "
            f"are now orphaned. If that is not what you expected, check the "
            f"league ID before syncing anything else."
        )
    return result


def sync_rosters(cfg: Mapping[str, Any], pool: Sequence[Player]) -> dict[str, Any]:
    adapter = adapter_for(cfg)
    remote = adapter.fetch_rosters(_league_key(cfg))
    mapping = team_map(cfg)
    known = {p.player_id for p in pool}

    written = 0
    unmatched_teams: list[str] = []
    unknown_players: list[str] = []
    for platform_team, player_ids in remote.items():
        our_team = resolve_team(mapping, platform_team)
        if our_team is None:
            unmatched_teams.append(str(platform_team))
            continue
        keep = [pid for pid in player_ids if pid in known]
        unknown_players.extend(pid for pid in player_ids if pid not in known)
        store.set_roster(cfg["id"], our_team, keep)
        written += 1

    return {
        "teams_written": written,
        "unmatched_teams": unmatched_teams,
        "players_not_in_pool": sorted(set(unknown_players)),
        "hint": _team_hint(cfg, unmatched_teams),
    }


def sync_scoreboard(cfg: Mapping[str, Any], week: int) -> dict[str, Any]:
    """One week's scores, and the fixtures that produced them.

    The fixtures are stored on the config as the league's schedule, because a
    generated round robin is a guess and the platform's own pairings are not.
    """
    adapter = adapter_for(cfg)
    board = adapter.fetch_scoreboard(_league_key(cfg), int(week))
    mapping = team_map(cfg)

    scores: dict[str, float] = {}
    unmatched: list[str] = []
    for platform_team, points in (board.get("scores") or {}).items():
        our_team = resolve_team(mapping, platform_team)
        if our_team is None:
            unmatched.append(str(platform_team))
            continue
        scores[our_team] = float(points)

    games = []
    for game in board.get("games") or []:
        home = resolve_team(mapping, game.get("home"))
        away = resolve_team(mapping, game.get("away"))
        if home and away:
            games.append({"home": home, "away": away})
        else:
            unmatched.extend(
                str(g) for g, r in ((game.get("home"), home), (game.get("away"), away))
                if r is None
            )

    if scores:
        store.set_week_scores(cfg["id"], int(week), scores)

    return {
        "week": int(week),
        "teams_scored": len(scores),
        "games": games,
        "final": bool(board.get("final")),
        "unmatched_teams": sorted(set(unmatched)),
        "hint": _team_hint(cfg, unmatched),
    }


def merge_schedule(cfg: dict[str, Any], week: int, games: Sequence[Mapping[str, str]]) -> bool:
    """Record the platform's real fixtures for a week, replacing any guess."""
    if not games:
        return False
    schedule = [dict(entry) for entry in (cfg.get("schedule") or [])]
    schedule = [entry for entry in schedule if int(entry.get("week", -1)) != int(week)]
    schedule.append({"week": int(week), "games": [dict(g) for g in games]})
    schedule.sort(key=lambda entry: entry["week"])
    cfg["schedule"] = schedule
    return True


def sync_transactions(cfg: Mapping[str, Any], pool: Sequence[Player]) -> dict[str, Any]:
    """Import every league move that is not already recorded.

    Platform moves carry an ``external_id``; those are remembered in the
    stored note so re-running a sync updates nothing and duplicates nothing.
    """
    adapter = adapter_for(cfg)
    remote = adapter.fetch_transactions(_league_key(cfg))
    mapping = team_map(cfg)
    index = player_index(pool)

    already = {
        txn.note for txn in store.list_transactions(cfg["id"]) if txn.note
    }

    imported = 0
    skipped_existing = 0
    unmatched_teams: list[str] = []
    unmatched_players: list[str] = []

    for row in remote:
        marker = _marker(row.get("external_id"))
        if marker and marker in already:
            skipped_existing += 1
            continue

        team_id = resolve_team(mapping, row.get("team_id"))
        if team_id is None:
            unmatched_teams.append(str(row.get("team_id")))
            continue
        partner = resolve_team(mapping, row.get("partner_team_id"))

        adds, missing_a = _resolve_all(index, row.get("adds") or [])
        drops, missing_d = _resolve_all(index, row.get("drops") or [])
        unmatched_players.extend(missing_a + missing_d)
        if not adds and not drops:
            continue

        store.add_transaction(cfg["id"], Transaction(
            type=str(row.get("type") or "add"),
            team_id=team_id,
            partner_team_id=partner,
            adds=adds,
            drops=drops,
            week=row.get("week"),
            date=_iso(row.get("timestamp")),
            faab=row.get("faab"),
            note=marker,
        ))
        imported += 1

    return {
        "imported": imported,
        "already_recorded": skipped_existing,
        "unmatched_teams": sorted(set(unmatched_teams)),
        "players_not_in_pool": sorted(set(unmatched_players)),
        "hint": _team_hint(cfg, unmatched_teams),
    }


def _resolve_all(
    index: Mapping[str, str], references: Iterable[Any]
) -> tuple[list[str], list[str]]:
    found: list[str] = []
    missing: list[str] = []
    for reference in references:
        resolved = resolve_player(index, reference)
        if resolved:
            found.append(resolved)
        else:
            name = reference if isinstance(reference, str) else reference.get("name", "?")
            missing.append(str(name))
    return found, missing


def _marker(external_id: Any) -> str:
    return f"platform:{external_id}" if external_id else ""


def _iso(timestamp: Any) -> str:
    """Platform timestamps are seconds or milliseconds since the epoch."""
    from datetime import datetime, timezone

    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return ""
    if value > 1e11:                       # milliseconds
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def sync_draft(cfg: Mapping[str, Any], pool: Sequence[Player]) -> dict[str, Any]:
    """Import a completed draft as this app's pick list."""
    from .draft import Pick

    adapter = adapter_for(cfg)
    remote = adapter.fetch_draft_results(_league_key(cfg))
    mapping = team_map(cfg)
    index = player_index(pool)
    order = len({p.get("team_id") for p in remote}) or 1

    picks: list[Pick] = []
    unmatched_players: list[str] = []
    unmatched_teams: list[str] = []
    for row in remote:
        team_id = resolve_team(mapping, row.get("team_id"))
        if team_id is None:
            unmatched_teams.append(str(row.get("team_id")))
            continue
        player_id = resolve_player(index, row)
        if player_id is None:
            unmatched_players.append(str(row.get("name") or row.get("player_key")))
            continue
        overall = int(row.get("pick") or 0)
        rnd = int(row.get("round") or 1)
        picks.append(Pick(
            overall=overall,
            round=rnd,
            pick_in_round=overall - (rnd - 1) * order,
            team_id=team_id,
            player_id=player_id,
            player_name=str(row.get("name") or ""),
            pos=str(row.get("pos") or ""),
            price=row.get("price"),
        ))

    if picks:
        store.save_picks(cfg["id"], picks)

    return {
        "picks": len(picks),
        "unmatched_teams": sorted(set(unmatched_teams)),
        "players_not_in_pool": sorted(set(unmatched_players)),
    }
