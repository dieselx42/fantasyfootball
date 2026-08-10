# Architecture and the path to hosting it

## The stack today

| Layer | Choice | Lines |
| --- | --- | --- |
| Language | Python 3.9+ | ~6,400 |
| Dependencies | **none** locally — pure standard library | 0 |
| HTTP server | `http.server.ThreadingHTTPServer` + a decorator router | ~1,000 |
| Season state | SQLite via stdlib `sqlite3`, or Postgres when hosted | ~1,300 |
| League rules | JSON files under `leagues/`, or a `JSONB` column | — |
| Frontend | Vanilla HTML/CSS/JS, no build step | ~1,930 |
| Tests | stdlib `unittest` | ~1,780 |

Verify the zero-dependency claim yourself:

```bash
grep -rhoE "^(import|from) [a-zA-Z_][a-zA-Z0-9_.]*" ff/ run.py --include=*.py \
  | awk '{print $2}' | cut -d. -f1 | sort -u
```

Everything it prints is either `ff` or stdlib.

### Why no framework

The constraint that drove this was draft night. A live draft gives you roughly
90 seconds per pick, and the cost of the app not starting at 5:44pm is losing
the draft. `python3 run.py` on any machine with Python — no venv, no `pip
install`, no Node, no build step, nothing to go stale between now and then — is
worth more than the ergonomics a framework would buy.

That is a *local-first* decision, not an anti-framework one. It stops paying
for itself the moment this becomes a hosted service, which is exactly what the
migration below is about.

## The shape that matters

The code is deliberately split into a pure core and a thin shell:

```
        pure functions, no I/O, no globals          I/O and state
   ┌────────────────────────────────────────┐   ┌──────────────────┐
   │ scoring_vocab · scoring · valuation    │   │ config (load)    │
   │ roster · draft · trades                │   │ store            │
   │ weekly · waivers · matchups            │   │ storage/         │
   │ transactions                           │   │ players (csv)    │
   │                                        │   │ platforms/       │
   │ config: schema, validation, derived    │   │ web/server       │
   │ views (starters_by_position, …)        │   │                  │
   └────────────────────────────────────────┘   └──────────────────┘
              ~2,800 lines. Ports unchanged.       Gets replaced.
```

Every engine function takes a config dict and a list of players and returns a
value. No module-level mutable state, no database handles, no filesystem
access. That is the expensive, hard-won part — the VOR baselines, tier
detection, run-risk model, trade search, marginal-lineup waiver valuation —
and **none of it changes when this becomes multi-user.**

The in-season engines were added to this side of the line on purpose, and it
shows in what they *don't* do. `weekly` takes a pool and returns a pool.
`waivers` takes a roster and a list of free agents. `matchups` takes a config
and a `{week: {team: points}}` mapping. `transactions` takes a list of moves
and a starting roster and returns the resulting one. None of them knows where
any of that came from, which is why the same code answers a question about
week 3 whether the scores were typed in, imported, or one day pulled from
Yahoo.

## What blocks multi-user today

Concrete, and all in the shell:

| # | Blocker | Where | Severity |
| --- | --- | --- | --- |
| 1 | One Yahoo token file for the whole process — a second user's OAuth overwrites the first's | `ff/platforms/yahoo.py` `TOKEN_PATH` | **Critical.** Cross-user credential collision, not just a bug. |
| 2 | Yahoo OAuth uses the `oob` copy-paste flow | `ff/platforms/yahoo.py` `REDIRECT_URI` | Correct locally, wrong hosted — needs a real redirect URI. |
| 3 | League configs are files in one shared directory with no owner | `ff/config.py` `LEAGUES_DIR` | Every user would see every league. |
| 4 | Projection CSVs live in one shared directory | `ff/players.py` `PROJECTIONS_DIR` | One user's upload changes everyone's valuations. |
| 5 | No `user_id` anywhere; tables key on `league_id` only | `ff/storage/files.py` and `ff/storage/postgres.py` `SCHEMA` | Needs an ownership column and a join. |
| 6 | No authentication, sessions, or CSRF protection | `ff/web/server.py` | The API is wide open by design. |
| 7 | `http.server` is a development server | `ff/web/server.py` `serve()` | No TLS, no graceful restart, limited concurrency. |
| 8 | In-process pool cache | `ff/pool.py` `_CACHE` | Correct (league-keyed) but not shared across workers. |
| 9 | SQLite under concurrent writers | `ff/storage/files.py` | Fine for one user; contended for many. Already solved when `DATABASE_URL` is set. |

Note what is *not* on that list: none of the scoring, valuation, draft or trade
logic. The migration is a shell replacement.

### Already done: step one

Every path the app writes to now resolves through `ff/paths.py` and honours
environment overrides, so two instances can run against entirely separate data:

```bash
FF_DATA_DIR=~/ff/keg-south    FF_LEAGUES_DIR=~/ff/keg-south/leagues    python3 run.py --port 8777
FF_DATA_DIR=~/ff/other-league FF_LEAGUES_DIR=~/ff/other-league/leagues python3 run.py --port 8778
```

That is useful today for keeping leagues apart, and it collapses blockers 3–5
into "change four constants" rather than "grep the codebase for open()".

## Deploying: why Vercel is the wrong shape for this app

Vercel genuinely supports Python — 3.12/3.13/3.14, ASGI and WSGI apps, and
even plain `BaseHTTPRequestHandler` handlers, which is what `ff/web/server.py`
already is. The HTTP layer is not the problem.

The problem is that **Vercel functions run on a read-only filesystem with an
ephemeral `/tmp` that does not survive between invocations.** This app writes
on every meaningful action:

| What | Where | Storage | On Vercel |
| --- | --- | --- | --- |
| League configs | `ff/config.py` `save()` | JSON file per league | read-only FS → `EROFS` |
| Projection CSVs | `ff/web/server.py` import | uploaded file | read-only FS → `EROFS` |
| Draft picks | `ff/store.py` `connect()` | SQLite | `/tmp` wiped between calls |
| Rosters + trades | `ff/store.py` `connect()` | SQLite | `/tmp` wiped between calls |
| Yahoo tokens | `ff/platforms/yahoo.py` | `secrets/*.json` | read-only FS → `EROFS` |

Every one of them breaks. Saving a league setting would throw; a draft pick
would vanish before the next request. Serverless is stateless by design, and
this app is stateful by nature — the draft board *is* accumulated state.

Deploying to Vercel is therefore not "push and go". It requires completing
phases 1–2 below (swap every store for a hosted database) *before* anything
works at all. That is real work, and it buys nothing that a container host
does not already give you.

### The alternative: a container host

Render, Fly.io and Railway all deploy from a git push exactly like Vercel,
but give you a long-running process and a persistent disk. SQLite keeps
working, the JSON configs keep working, and the app deploys essentially as it
stands today — a `Dockerfile` and a mount point.

| | Vercel | Render / Fly / Railway |
| --- | --- | --- |
| Deploy from git | yes | yes |
| Persistent disk | **no** | yes |
| Long-running process | no | yes |
| Works with today's code | **no** | yes, nearly as-is |
| Prerequisite work | phases 1–2 first | a Dockerfile |
| Cost at this scale | free tier | ~$0–7/month |

**Recommendation: use a container host.** Reach for Vercel only if you also
want the multi-user rewrite now, since that is the price of admission. If the
end state is many users, phases 1–2 have to happen eventually either way —
Vercel just forces them to happen first, before you get anything running.

## Recommended target stack

**Stay on Python.** The engine is the asset; a rewrite in another language
throws away the only genuinely hard part to buy nothing.

| Layer | Recommendation | Why |
| --- | --- | --- |
| API | **FastAPI** + uvicorn | Async, automatic OpenAPI, Pydantic validation. The existing decorator routes map over almost mechanically. |
| Database | **Postgres** | Real concurrency. Crucially, league configs are *already* JSON documents — they drop into a `JSONB` column unchanged, so `config.py`'s schema and validation survive as-is. |
| File uploads | Postgres `bytea` at first; S3/R2 if volume grows | Projection CSVs are ~10–200 KB. Not worth object storage on day one. |
| Auth | OAuth sign-in (Google) or magic links, cookie sessions | Avoids storing passwords entirely. |
| Secrets | Per-user encrypted rows for platform tokens | Fixes blocker 1 properly. |
| Frontend | Keep the vanilla JS at first | 1,460 lines with no build step is an asset, not debt. Move to Svelte or React only when the UI genuinely outgrows it. |
| Hosting | One container on Fly.io, Render, or Railway | A single always-on instance with a managed Postgres is the whole infrastructure. |

Estimated cost at small scale: **$0–15/month.**

## Migration phases

Each phase leaves a working app; none requires touching the engine.

**Phase 1 — storage seam.** Introduce `LeagueStore` and `ProjectionStore`
interfaces with the current file-backed implementations behind them. Callers
stop importing directories. *No behaviour change, fully testable.*

**Phase 2 — Postgres implementations.** Add `users` and `leagues` tables with
an `owner_id`, league config in `JSONB`. Add a second implementation of each
interface. Local mode keeps using files; hosted mode uses Postgres.

**Phase 3 — FastAPI + auth.** Port routes, add sign-in and sessions, scope
every query to the signed-in user. Serve the same static frontend.

**Phase 4 — per-user platform tokens.** Move Yahoo credentials into encrypted
per-user rows and switch OAuth from `oob` to a hosted redirect URI.

**Phase 5 — sharing.** Once leagues have owners, "invite your league-mates"
becomes a permissions row: commissioner edits the rules, everyone else reads
them and manages their own team.

Phases 1 and 2 are the load-bearing ones — roughly a day of work each. Phase 3
is mostly mechanical. Nothing here needs to happen before your draft.

## The recommendation on timing

Your draft is **Sunday Aug 30, 5:45pm EDT**. Hosting this for other people and
drafting well are unrelated pieces of work, and only one of them has a
deadline. The engine is already built so that doing the draft work first costs
the hosting work nothing — every feature added between now and Aug 30 lands in
the pure core, which ports unchanged.

Draft first. Host after.
