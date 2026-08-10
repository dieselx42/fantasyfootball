# Architecture and the path to hosting it

## The stack today

| Layer | Choice | Lines |
| --- | --- | --- |
| Language | Python 3.9+ | ~3,600 |
| Dependencies | **none** — pure standard library | 0 |
| HTTP server | `http.server.ThreadingHTTPServer` + a decorator router | ~600 |
| Season state | SQLite via stdlib `sqlite3` | ~190 |
| League rules | JSON files under `leagues/` | — |
| Frontend | Vanilla HTML/CSS/JS, no build step | ~1,460 |
| Tests | stdlib `unittest` | ~520 |

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
   │ roster · draft · trades                │   │ store (sqlite)   │
   │                                        │   │ players (csv)    │
   │ config: schema, validation, derived    │   │ platforms/       │
   │ views (starters_by_position, …)        │   │ web/server       │
   └────────────────────────────────────────┘   └──────────────────┘
              ~1,500 lines. Ports unchanged.       Gets replaced.
```

Every engine function takes a config dict and a list of players and returns a
value. No module-level mutable state, no database handles, no filesystem
access. That is the expensive, hard-won part — the VOR baselines, tier
detection, run-risk model, trade search — and **none of it changes when this
becomes multi-user.**

## What blocks multi-user today

Concrete, and all in the shell:

| # | Blocker | Where | Severity |
| --- | --- | --- | --- |
| 1 | One Yahoo token file for the whole process — a second user's OAuth overwrites the first's | `ff/platforms/yahoo.py` `TOKEN_PATH` | **Critical.** Cross-user credential collision, not just a bug. |
| 2 | Yahoo OAuth uses the `oob` copy-paste flow | `ff/platforms/yahoo.py` `REDIRECT_URI` | Correct locally, wrong hosted — needs a real redirect URI. |
| 3 | League configs are files in one shared directory with no owner | `ff/config.py` `LEAGUES_DIR` | Every user would see every league. |
| 4 | Projection CSVs live in one shared directory | `ff/players.py` `PROJECTIONS_DIR` | One user's upload changes everyone's valuations. |
| 5 | No `user_id` anywhere; tables key on `league_id` only | `ff/store.py` `SCHEMA` | Needs an ownership column and a join. |
| 6 | No authentication, sessions, or CSRF protection | `ff/web/server.py` | The API is wide open by design. |
| 7 | `http.server` is a development server | `ff/web/server.py` `serve()` | No TLS, no graceful restart, limited concurrency. |
| 8 | In-process pool cache | `ff/pool.py` `_CACHE` | Correct (league-keyed) but not shared across workers. |
| 9 | SQLite under concurrent writers | `ff/store.py` | Fine for one user; contended for many. |

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
