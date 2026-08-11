# Fantasy Football Assistant

A local web app for drafting a fantasy football team and running it through the
season. It scores players under **your** league's rules, ranks them by value
over replacement, runs a live draft board, sets your weekly lineup around byes
and injuries, works the waiver wire, tracks what every other manager in the
league is doing, and suggests trades your league would actually approve.

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

### Running more than one instance

Every path the app writes to honours an environment override, so separate
leagues (or separate people sharing a machine) can run side by side without
touching each other's data:

```bash
FF_DATA_DIR=~/ff/keg-south    FF_LEAGUES_DIR=~/ff/keg-south/leagues    python3 run.py --port 8777
FF_DATA_DIR=~/ff/other-league FF_LEAGUES_DIR=~/ff/other-league/leagues python3 run.py --port 8778
```

| Variable | Default | Holds |
| --- | --- | --- |
| `FF_DATA_DIR` | `./data` | Database and uploaded projections |
| `FF_LEAGUES_DIR` | `./leagues` | League config JSON |
| `FF_PROJECTIONS_DIR` | `$FF_DATA_DIR/projections` | Projection CSVs |
| `FF_DB_PATH` | `$FF_DATA_DIR/fantasy.db` | Season state |
| `FF_SECRETS_DIR` | `./secrets` | Platform OAuth tokens |

### Hosting

The same codebase runs hosted. Set `DATABASE_URL` and storage switches from
local files to Postgres; everything else is identical. See
[DEPLOY.md](DEPLOY.md) for the Vercel + Supabase walkthrough, and
[ARCHITECTURE.md](ARCHITECTURE.md) for why it is built this way.

| | Local | Hosted |
| --- | --- | --- |
| Command | `python3 run.py` | Vercel serverless |
| Storage | JSON + SQLite | Postgres |
| Dependencies | none | `psycopg2-binary` |

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

## The season

The draft is one night; the season is four months. Two tabs cover it.

### Week

Lineup and waiver wire together, because they are one decision — the question
is never "who starts" or "who do I add" in isolation, it is *what is the best
team I can field on Sunday*.

- **Availability is a number, not a special case.** A player on bye scores
  zero; one ruled out scores zero; a questionable one is discounted. Because
  that lands in his projection, the existing lineup optimiser benches him on
  its own. There is no start/sit rule anywhere in the codebase.
- **Sunday 11:28.** A beat writer reports someone inactive. Set his status on
  his row and the lineup re-solves immediately — no re-import, and the
  override is scoped to that week so it never leaks into the next one.
- **The waiver wire is ranked on what it changes, not on who is best.** The
  headline number is marginal starting-lineup gain: your best lineup with the
  player minus your best lineup without him. A genuinely good WR who would sit
  behind three better ones is worth *zero*, and says so.
- Two horizons are scored separately, because they disagree constantly and the
  disagreement is the point: a handcuff whose starter just went down is worth
  nothing this week and a great deal for the rest of the season.
- With a full roster every add is paired with **the drop that makes room for
  it**, and the gain is measured after the swap. The suggested drop is the one
  that costs least — usually not your worst player, but your worst player at a
  position you are deep in.
- FAAB leagues get a **bid**, anchored on how much the add improves your
  starting lineup as a fraction of what that lineup already scores, scaled by
  `waivers.bid_aggressiveness` and never more than 60% of what you have left.

### Results

- **Scoreboard** — every game in the week, projected before kickoff and final
  after it, so you know which matchups are close while you can still do
  something about it.
- **Standings** — records, points for and against, streaks, and the playoff
  line drawn where your config puts it. Leagues that play against the median
  get that second weekly result counted too.
- **League activity** — every add, drop and trade in the league, not just
  yours, plus which positions the league is chasing. That last one is a sell
  signal: if four managers spent the week buying running backs, yours are
  worth more than they were on Tuesday.

Transactions are an append-only log, and current rosters are *derived* by
replaying it over the draft result. So a mistyped add is undone by deleting
one row rather than rebuilding a roster by hand, and the history survives
either way.

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

## The League tab

Everything about a league is entered and edited here — nothing is hardcoded,
so setting up a second league for a different group is the same work as the
first:

- **League info** — name, season, platform, platform league ID
- **Draft schedule** — date, time, UTC offset, timezone label, how early to
  arrive, location, notes, draft type, rounds, seconds per pick
- **Draft order** — round 1 order, reorderable; snake reverses it each round
- **Scoring** — every stat, grouped, with per-unit values
- **Scoring bonuses** — yardage thresholds, stackable
- **Points-allowed bands** — defense scoring by band
- **Roster slots** and bench size
- **Waivers and playoffs** — type, timing, playoff field and weeks
- **Trade rules** — approval, deadline, package size, fairness gap
- **Teams and managers**, and which one is yours

A live countdown to the draft sits in the header on every screen. The time is
stored with its UTC offset, because a draft time without a timezone is the
easiest possible way to miss a draft.

### Scoring that multipliers can't express

Two kinds of rule need more than a points-per-unit number, and both are
configurable:

**Stacking bonuses.** Yahoo writes passing yards as "50 yards per point;
3 points at 300 yards; 4 points at 400 yards". That is `pass_yd: 0.02` plus two
bonus rows. They stack — a 400-yard game earns both, for +7.

**Banded stats.** Defensive points allowed is a lookup, not a multiplier: 0
allowed scores 10, 1–6 scores 7, and so on. `scoring_bands` holds an ascending
list of `{"max": <inclusive bound>, "points": n}` ending in an open-ended band:

```jsonc
"scoring_bands": {
  "dst_pa": [
    { "max": 0,    "points": 10 },
    { "max": 6,    "points": 7  },
    { "max": 13,   "points": 4  },
    { "max": 20,   "points": 2  },
    { "max": 27,   "points": 0  },
    { "max": 34,   "points": -1 },
    { "max": null, "points": -4 }
  ]
}
```

Validation rejects a band list that does not end open-ended, since otherwise
some values would silently score nothing.

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
  },
  "waivers": { "type": "faab", "faab_budget": 100, "bid_aggressiveness": 4 },
  "playoffs": { "teams": 6, "weeks": [15, 16, 17] },
  "valuation": {
    // How the waiver board blends "helps this Sunday" against "helps in
    // November" against "is simply the best player left".
    "waiver_weights": { "week": 1.0, "rest_of_season": 0.6, "upside": 0.15 },
    // What share of a projection each injury status leaves behind.
    "status_multipliers": { "Q": 0.92, "D": 0.25 }
  },
  // Optional. Absent, a round robin is generated; paste your platform's real
  // fixtures here and the standings match the site exactly.
  "schedule": [
    { "week": 1, "games": [{ "home": "diesel", "away": "code-brown" }] }
  ]
}
```

Two configs ship as references:

- `leagues/example-league.json` — a generic 12-team half-PPR league.
- `leagues/keg-south.json` — a real 10-team Yahoo league, useful precisely
  because its rules are *not* standard: 6-point passing TDs, full PPR,
  yards-per-point scoring, tiered field-goal misses, and banded points
  allowed. If a change breaks nothing in the example league but breaks this
  one, the change was wrong.

To support a stat nobody has needed yet, add it to `ff/scoring_vocab.py`. It
then appears in the scoring editor and works everywhere; no other file changes.

## Platforms

Connecting a platform is optional — everything works typed in by hand — but it
turns a lot of weekly data entry into one button.

| | Manual | Sleeper | Yahoo | ESPN |
| --- | :-: | :-: | :-: | :-: |
| Teams and managers | — | ✓ | ✓ | ✓ |
| Scoring and roster rules | — | ✓ | ✓ | — |
| Player universe | ✓ | ✓ | ✓ | ✓ |
| Current rosters | — | ✓ | ✓ | ✓ |
| Weekly scores | — | ✓ | ✓ | — |
| League transactions | — | ✓ | ✓ | — |
| Draft results | — | — | ✓ | — |
| Needs credentials | no | no | your own app | cookies if private |

Nothing in this table is hardcoded in the UI. Each adapter *declares* what it
can do, and the sync panel is built from that declaration — so a half-finished
integration offers exactly the buttons it can honour instead of failing on one
it can't.

### Importing your league's rules

The highest-value sync is **Scoring and roster rules**, because it removes the
most error-prone setup step there is: retyping a scoring page. It reads your
league's real settings and shows them as a diff against your config before
writing anything, since scoring drives every valuation in the app.

Two details worth knowing:

- **Scoring is matched by name, not by internal id.** Yahoo identifies stats
  numerically, and those numbers are stable but undocumented. Mapping them by
  hardcoded id means that the day one is wrong, every number in the app is
  quietly wrong too. So the adapter reads Yahoo's own stat-category names —
  the same words on your league's settings page — and matches those.
- **Anything untranslatable is named, never dropped.** A rule this app has no
  concept of is listed in the result with a reason. A missing scoring rule
  that vanished silently would change every valuation with nothing on screen
  to explain why.

Points-allowed scoring is folded up on the way in: platforms express it as one
stat per band ("Points Allowed 1-6", "7-13", …), and it arrives here as a
single banded rule.

### Sync teams first

Platform team ids ("1", `nfl.l.184206.t.7`) mean nothing to a league you typed
in. Syncing **Teams and managers** records each team's platform key, and every
later sync joins on it. Sync anything else first and it will tell you that
nothing matched, and why.

Transactions carry the platform's own id, so re-running a sync imports what is
new and skips what you already have.

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

### Wiring your own platform

Implement `PlatformAdapter` in `ff/platforms/`, register it in
`ff/platforms/__init__.py`, and it appears in the setup wizard and the sync
panel automatically. The draft board, valuation, waiver and trade engines
never learn it exists.

You do not have to implement all of it. Declare what you support and the rest
stays hidden:

```python
class MyLeagueAdapter(PlatformAdapter):
    kind, label = "myleague", "My League"
    setup_fields = (("league_id", "League ID", "From your league URL."),)

    def capabilities(self):
        return {"league", "rosters", "scoreboard"}   # and no more

    def fetch_scoreboard(self, league_id, week):
        return {"week": week,
                "games": [{"home": "1", "away": "2"}],
                "scores": {"1": 118.42, "2": 102.8},
                "final": True}
```

Anything you don't implement raises `PlatformUnsupported`, and the app says
"My League does not provide draft results" rather than showing an error.

Three conventions make an adapter join up with the rest of the app:

| Return | Shape | Why |
| --- | --- | --- |
| Rosters | *our* player ids (`ff.players.make_player_id`) | Platform ids join to no projection, so lineups come back empty |
| Transaction players | `{"name", "pos", "team"}` | Our ids derive from name and position, so the sync layer rebuilds an exact one |
| `fetch_rules` | config blocks plus an `unmapped` list | A rule you couldn't translate has to be visible, not absent |

Team ids can be whatever the platform uses — `sync.team_map` joins them to
your config through the `platform_key` recorded when teams are imported.

Tests go against recorded payload shapes rather than the live API; see
`tests/fixtures/yahoo_payloads.py` for the pattern. Sleeper's API needs no
auth, so `tests/test_platforms.py` also carries an opt-in live check:

```bash
FF_TEST_LIVE_SLEEPER=1 python3 -m unittest tests.test_platforms
```

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
  weekly.py             season projections -> one week, byes and injuries
  waivers.py            free agent value, add/drop pairing, FAAB bids
  matchups.py           schedule, weekly results, standings
  transactions.py       the league's roster moves, and replaying them
  sync.py               platform data -> our ids, our storage, with a report
  trades.py             trade evaluation, legality, suggestion search
  pool.py               scored+valued player pool, cached
  store.py              persistence for picks, rosters, scores, the log
  storage/              file and Postgres backends behind one interface
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
- **Syncing is a button, not a background job.** Scores, transactions and
  rules are pulled when you ask for them; nothing polls. Weekly *projections*
  and injury statuses are still yours to import or set — no platform publishes
  projections worth trusting.
- **The Yahoo adapter has not been exercised against the live API.** Its
  parsers are covered against recorded payload shapes, and the rules importer
  reproduces this repo's real `keg-south.json` scoring exactly, but the first
  live sync is worth eyeballing before trusting it. Sleeper's path is
  verified against the real API.
- Without a week-specific projection file, weekly numbers are the season total
  divided across the schedule. That is a real number but a blunt one: it knows
  about byes and injuries and nothing about matchups. The Week tab flags when
  it is falling back to it.
- The generated schedule is a round robin, not your league's actual fixture
  list. Paste the real one into `schedule` in your league config if you want
  the standings to line up exactly with the site before the season ends.
