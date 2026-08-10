# Projections

Drop any projections CSV in this folder (or import one through the web UI
under **League › Projections**). Every `.csv` here is loaded and merged.

## What gets read

Column headers are matched loosely, so most exports work without editing.
The importer recognises common variants — `Player` / `Player Name` / `Name`,
`Rec Yds` / `Receiving Yards` / `RecYds`, and so on. Unknown columns are
ignored rather than treated as an error.

The only required column is the player name. Everything else improves the
output:

| Column | Why it matters |
| --- | --- |
| `Pos` | Required for valuation. `D/ST`, `DEF` and `DST` all work. |
| `Team` | Used for display and to disambiguate similar names. |
| `Bye` | Shown on the draft board, and scores the player zero that week. |
| `ADP` | Drives the "will he last until my next pick?" calculation. |
| `Week` | Makes this a weekly projection rather than a season one — see below. |
| `Status` | Availability: `Out`, `Questionable`, `IR`, `Doubtful`, … |
| `Opp` | This week's opponent. Display only, but it is the context you want. |
| Stat columns | Passing/rushing/receiving/kicking/defense volume. |

## Season files and weekly files

A file with no `Week` column is a **season** projection: the draft board and
the trade engine use it, and the weekly views fall back to it by dividing the
total across the schedule. That fallback is honest but blunt — a per-game
average knows nothing about who a player faces on Sunday, and the Week tab
says so in a banner whenever it is being used.

A file **with** a `Week` column is a weekly projection, and rows in it override
the average for that week only. The two kinds never collide: a week 3 row and a
season row for the same player are stored separately, so importing weekly
numbers can never corrupt your draft board.

```csv
Player,Team,Pos,Opp,Status,Week,Rush Yds,Rush TD,Rec,Rec Yds
Bijan Robinson,ATL,RB,TB,,3,96,1,4,31
```

Status can also be set per player per week from the Week tab, which is the
path to use when someone is ruled out an hour before kickoff. That override
wins over whatever the file said, and applies to that week alone.

## Stat lines beat precomputed points

If a file has raw stat columns (`Pass Yds`, `Rec`, `Rush TD`, …) the app
re-scores every player under **your** league's rules. That is the whole point:
a 100-catch receiver is worth 50 more points in full PPR than in half, and no
generic ranking list knows which one you play.

A `Points` / `FPTS` column is used only as a fallback when a file has no stat
columns at all. Those numbers were computed under someone else's scoring
settings, so players sourced that way are marked `(source points)`.

## Where to get free projections

- **FantasyPros** — free account, CSV export of consensus projections
  (this is the most common source, and it exports full stat lines).
- **nflverse** — open play-by-play and roster data on GitHub, if you want to
  build your own projections.
- **Sleeper** — free public API with no key required. The app can pull the
  player universe (names, teams, positions) from it directly; useful for
  filling in players a projections file is missing.

## About `sample_projections.csv`

The bundled sample exists so the app is usable the moment it starts, and so
the test suite has something to run against.

**Its numbers are illustrative placeholders, not real projections.** The player
names and teams are real; the stat lines are hand-written approximations of a
plausible season shape. Replace this file with a real source before you draft.
