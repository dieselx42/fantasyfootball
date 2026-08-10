"""The stat vocabulary every league config is written against.

This module is the contract between three things that must agree:

  * projection sources (a CSV column, a platform API field)
  * a league's ``scoring`` block (a stat key -> points-per-unit multiplier)
  * the scoring engine

Adding support for a new stat means adding it here once. Nothing else in the
codebase hardcodes a stat name, so a league that scores something unusual
(completions, return yards, tackles for a IDP league) is a config change.
"""

from __future__ import annotations

# Canonical stat keys grouped by the position family that produces them.
# The grouping is only used to lay out the scoring editor in the web UI.
STAT_GROUPS: dict[str, list[tuple[str, str]]] = {
    "Passing": [
        ("pass_yd", "Passing yards"),
        ("pass_td", "Passing TD"),
        ("pass_int", "Interception thrown"),
        ("pass_cmp", "Completion"),
        ("pass_inc", "Incompletion"),
        ("pass_att", "Pass attempt"),
        ("pass_2pt", "Passing 2PT conversion"),
        ("pass_sacked", "Sack taken"),
    ],
    "Rushing": [
        ("rush_yd", "Rushing yards"),
        ("rush_td", "Rushing TD"),
        ("rush_att", "Rush attempt"),
        ("rush_2pt", "Rushing 2PT conversion"),
    ],
    "Receiving": [
        ("rec", "Reception"),
        ("rec_yd", "Receiving yards"),
        ("rec_td", "Receiving TD"),
        ("rec_tgt", "Target"),
        ("rec_2pt", "Receiving 2PT conversion"),
    ],
    "Misc": [
        ("fum_lost", "Fumble lost"),
        ("fum", "Fumble (any)"),
        ("ret_td", "Kick/punt return TD"),
        ("off_fum_ret_td", "Offensive fumble return TD"),
        ("two_pt", "2PT conversion (generic)"),
    ],
    "Kicking": [
        ("fg_0_19", "FG made 0-19"),
        ("fg_20_29", "FG made 20-29"),
        ("fg_30_39", "FG made 30-39"),
        ("fg_40_49", "FG made 40-49"),
        ("fg_50p", "FG made 50+"),
        ("fg_miss", "FG missed (any distance)"),
        ("fg_miss_0_19", "FG missed 0-19"),
        ("fg_miss_20_29", "FG missed 20-29"),
        ("fg_miss_30_39", "FG missed 30-39"),
        ("fg_miss_40_49", "FG missed 40-49"),
        ("fg_miss_50p", "FG missed 50+"),
        ("xp_made", "Extra point made"),
        ("xp_miss", "Extra point missed"),
    ],
    "Defense / Special Teams": [
        ("dst_sack", "Sack"),
        ("dst_int", "Interception"),
        ("dst_fum_rec", "Fumble recovery"),
        ("dst_td", "Defensive/ST TD"),
        ("dst_safety", "Safety"),
        ("dst_block", "Blocked kick"),
        ("dst_ret_td", "Kickoff/punt return TD"),
        ("dst_xp_ret", "Extra point returned"),
        ("dst_pa", "Points allowed (per point)"),
        ("dst_ya", "Yards allowed (per yard)"),
    ],
}

#: Stats that carry a *raw total* used to look up a scoring band rather than
#: being multiplied by a per-unit value. See ``config["scoring_bands"]``.
BAND_STATS: dict[str, str] = {
    "dst_pa": "Points allowed",
    "dst_ya": "Yards allowed",
}

# Flat lookup: stat key -> human label.
STAT_LABELS: dict[str, str] = {
    key: label for group in STAT_GROUPS.values() for key, label in group
}

ALL_STATS: tuple[str, ...] = tuple(STAT_LABELS)


def is_stat(key: str) -> bool:
    return key in STAT_LABELS
