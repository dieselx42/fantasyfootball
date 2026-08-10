"""Turn a stat line into fantasy points under a league's scoring rules.

This is deliberately tiny. It is the only place that knows how points are
computed, and it reads every rule from the league config, so two leagues with
different rules run the same code over different data.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def score_stats(stats: Mapping[str, float], cfg: Mapping[str, Any]) -> float:
    """Fantasy points for one stat line under ``cfg``."""
    rules = cfg.get("scoring") or {}
    total = 0.0
    for stat, value in stats.items():
        multiplier = rules.get(stat)
        if multiplier:
            total += float(value) * float(multiplier)

    for bonus in cfg.get("scoring_bonuses") or []:
        stat = bonus.get("stat")
        if stat is None:
            continue
        value = float(stats.get(stat, 0.0))
        threshold = float(bonus.get("threshold", 0))
        if value >= threshold:
            if bonus.get("repeating"):
                # e.g. +1 every 10 rushing yards over 100
                step = float(bonus.get("threshold") or 1) or 1.0
                total += float(bonus.get("points", 0)) * int(value // step)
            else:
                total += float(bonus.get("points", 0))

    return round(total, 2)


def explain(stats: Mapping[str, float], cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per-stat breakdown, so the UI can show *why* a projection is what it is."""
    rules = cfg.get("scoring") or {}
    rows = []
    for stat, value in stats.items():
        multiplier = rules.get(stat)
        if not multiplier or not value:
            continue
        rows.append(
            {
                "stat": stat,
                "value": round(float(value), 2),
                "per_unit": float(multiplier),
                "points": round(float(value) * float(multiplier), 2),
            }
        )
    rows.sort(key=lambda r: abs(r["points"]), reverse=True)
    return rows


def score_many(
    lines: Iterable[Mapping[str, float]], cfg: Mapping[str, Any]
) -> list[float]:
    return [score_stats(line, cfg) for line in lines]
