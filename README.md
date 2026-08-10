# Fantasy Football Assistant

A local web app for drafting a fantasy football team and running it through the
season. It scores players under **your** league's rules, ranks them by value
over replacement, runs a live draft board, sets your weekly lineup, and
suggests trades that your league would actually approve.

The central design decision: **league logic is data, not code.** Every rule —
scoring, roster slots, draft format, trade constraints — lives in a JSON file
under `leagues/`. Running a different group's league means a different file,
not a different program.

## Running it

No install, no dependencies, no build step. Python 3.9+ and nothing else:

```bash
python3 run.py
```

That opens <http://127.0.0.1:8777>. To reach it from your phone on the same
wifi during a draft: `python3 run.py --host 0.0.0.0`.

Tests:

```bash
python3 -m unittest discover -s tests
```

## First run

The setup wizard asks three things:

1. **Platform** — Yahoo, Sleeper, ESPN, or manual. Yahoo needs a free
   developer app (see below). Manual works immediately and supports every
   feature; platform adapters only save typing.
2. **League basics** — name, team count, PPR setting, draft rounds.
3. **Teams and managers** — one row each, so the draft board and trade engine
   know who owns what.

Then import projections under **League › Projections** and you're ready.

## How the valuation works

Raw projected points cannot be compared across positions. A QB projected for
340 is not better than an RB projected for 260, because every team in your
league can start a ~300-point QB, while a 260-point RB might be the best one
left on the board.

So every player is measured against **replacement level** — what you could get
for free at that position — and that baseline is derived from your own roster
settings:

```
replacement level = the (teams × starters at that position)-th best player
```

A 12-team league starting 2 RB + 1 FLEX has roughly 2.45 RB starters per team,
so the ~29th RB is replacement level. Change to superflex, or add a second TE
slot, and the baselines move automatically. Nothing is hardcoded.

On top of that:

- **Tiers** are found by looking for unusually large gaps in the points curve.
  The useful draft question is never "who is best?" but "will anyone from this
  group still be here at my next pick?"
- **Positional cliffs** show what you lose by waiting for the current tier to
  clear — the single most actionable number on a draft board.
- **Run risk** uses ADP and your snake position to estimate whether a player
  survives until you pick again.

## Draft board

The board recommends picks by blending three things a good drafter holds in
their head at once:

| Signal | Question it answers |
| --- | --- |
| Value over replacement | How much better is he than a freely available player? |
| Roster need | Do I actually have a hole here, or is this my third QB? |
| Run risk | Will he still be there when I pick again? |

Each is reported separately alongside the blended score, so you can disagree
with the weighting. Click any available player to draft them; the board tracks
every team's roster, not just yours, which is what makes the trade engine work
the moment the draft ends.

`Undo` fixes a misclick. Picks are stored as an ordered list and every derived
view is recomputed from it, so the board can never drift out of sync.

## Trades

Two directions, one engine:

- **Evaluate** a specific offer — values both packages, checks it against your
  league's rules, and reports whether each side's *starting lineup* improves.
- **Suggest** trades worth proposing — finds a position where you have surplus
  and a partner has a hole, and vice versa, then searches the pairings.

Gains are measured in value over replacement rather than raw points, so an
unfilled roster spot counts as replacement level instead of zero. Without that,
any trade into an empty slot scores as an enormous win.

Deals that exceed your league's configured fairness gap are marked **veto
risk** and sorted below cleaner offers — a bigger gain you can never get
approved is worth less than a smaller one you can.

## Changing the rules

Everything below is editable in the UI (**League** tab) and lives in
`leagues/<your-league>.json`:

```jsonc
{
  "scoring":  { "rec": 0.5, "pass_td": 4, "rush_yd": 0.1, "pass_int": -2 },
  "scoring_bonuses": [
    { "stat": "rush_yd", "threshold": 100, "points": 3 }
  ],
  "roster": {
    "slots": [
      { "slot": "QB",   "count": 1, "eligible": ["QB"] },
      { "slot": "FLEX", "count": 1, "eligible": ["RB", "WR", "TE"] }
    ],
    "bench": 6
  },
  "draft":  { "type": "snake", "rounds": 15 },
  "trades": {
    "approval": "league_vote",
    "veto_votes_required": 4,
    "deadline_week": 11,
    "max_players_per_side": 3,
    "allow_uneven": true,
    "fairness": { "max_value_gap_pct": 15, "require_both_improve": true }
  }
}
```

`leagues/example-league.json` is a filled-in 12-team half-PPR config with
yardage bonuses and a league-vote trade policy — copy it as a starting point.

To support a stat nobody has needed yet, add it to `ff/scoring_vocab.py`. It
then appears in the scoring editor and works everywhere; no other file changes.

## Platforms

| Platform | Status | Notes |
| --- | --- | --- |
| Manual | Works | No API. Every feature is available. |
| Sleeper | Works | Free public API, no key. Good source for player data even if your league is elsewhere. |
| Yahoo | Needs your app | OAuth2 only — see below. |
| ESPN | Partial | Public leagues read fine; private leagues need browser cookies. |

### Yahoo setup

Yahoo has no anonymous read path, so you need a free app of your own:

1. Create one at <https://developer.yahoo.com/apps/>
   - Application Type: **Installed Application**
   - Redirect URI: `oob`
   - API Permissions: **Fantasy Sports**, Read (or Read/Write to submit trades)
2. Paste the Client ID and Secret into the setup wizard.
3. Click **Get authorization link**, approve in the browser, and paste the code
   back.

Tokens are written to `secrets/yahoo.token.json`, which is gitignored and
chmod 600. They refresh automatically.

### Adding a platform

Implement `PlatformAdapter` in `ff/platforms/`, register it in
`ff/platforms/__init__.py`, and it appears in the setup wizard automatically.
The draft board, valuation and trade engines never learn it exists.

## Layout

```
run.py                  entrypoint
ff/
  config.py             league schema, validation, defaults, derived views
  scoring_vocab.py      the stat vocabulary configs are written against
  scoring.py            stat line -> fantasy points
  players.py            projection import, name matching, the Player model
  valuation.py          replacement level, VOR, tiers, scarcity
  roster.py             slot filling, optimal lineup, roster need
  draft.py              draft state, snake order, pick recommendations
  trades.py             trade evaluation, legality, suggestion search
  pool.py               scored+valued player pool, cached
  store.py              SQLite persistence for picks, rosters, saved trades
  platforms/            pluggable platform adapters
  web/                  HTTP server + the single-page frontend
leagues/                your league configs — this is the swappable part
data/projections/       drop CSVs here
tests/
```

## Known limits

- Projections are only as good as the file you import. The bundled sample is
  illustrative placeholder data, not real projections — replace it before
  drafting.
- Value over replacement is computed from the pool you load. If you import
  only 12 kickers, the 12th is treated as replacement level and kicker value
  is overstated. Import a full pool.
- Auction drafts are modelled in the config but the board is snake/linear only.
- Weekly scoring assumes season-long projections divided evenly; there is no
  week-by-week matchup or injury feed yet.
