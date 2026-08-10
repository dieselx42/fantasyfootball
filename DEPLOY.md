# Deploying to Vercel

The app runs in two modes from one codebase:

| | Local | Hosted |
| --- | --- | --- |
| Command | `python3 run.py` | Vercel serverless function |
| Server | stdlib `http.server` | WSGI (`ff/wsgi.py`) |
| Storage | JSON files + SQLite | Postgres |
| Dependencies | **none** | `psycopg2-binary` |

Both share one router, so the two front-ends cannot drift apart. Local stays
dependency-free, which is what keeps draft night free of package installs.

## Why Postgres is not optional

Vercel functions run on a **read-only filesystem** with an ephemeral `/tmp`
that does not survive between invocations. Every write this app performs —
saving a league setting, making a draft pick, importing projections, storing a
Yahoo token — fails there. Setting `DATABASE_URL` switches all of them to
Postgres. Without it, the deployment will start and then fail on the first
write.

---

## 1. Create the database (Supabase)

1. Create a project at <https://supabase.com> (free tier is enough).
2. Go to **Project Settings → Database → Connection string → URI**.
3. Choose the **Transaction pooler** connection (port `6543`), not the direct
   connection. Serverless functions open a connection per invocation, and the
   pooler is what keeps that from exhausting Postgres' connection limit.
4. Replace `[YOUR-PASSWORD]` with your database password.

It looks like:

```
postgresql://postgres.abcdefghijkl:PASSWORD@aws-0-us-east-1.pooler.supabase.com:6543/postgres
```

**No migration step is needed.** The app runs `CREATE TABLE IF NOT EXISTS` on
first use, so the six tables appear by themselves.

> `db/schema.sql` is a *different*, larger schema — the multi-user target with
> `owner_id`, league membership and row-level security. It is documentation for
> the sign-in work, not a prerequisite for this deploy. Don't run it yet.

## 2. Set environment variables in Vercel

**Project → Settings → Environment Variables.** Add both to *Production*,
*Preview* and *Development*:

| Variable | Value | Why |
| --- | --- | --- |
| `DATABASE_URL` | the Supabase pooler URI | switches storage to Postgres |
| `APP_PASSWORD` | any strong passphrase | see below |

### About `APP_PASSWORD`

There is no per-user sign-in yet, so **without a password the deployment is
world-writable** — anyone who finds the URL could clear your draft board
mid-draft. Setting `APP_PASSWORD` puts a single shared password in front of
everything, backed by a signed, HTTP-only session cookie.

This is a stopgap, not multi-user auth. It is one password shared by whoever
you give it to, and it has no concept of *which* user is asking. Google sign-in
with real per-user data is the next piece of work, and `db/schema.sql` is the
schema it lands on.

If you leave `APP_PASSWORD` unset the app runs open, which is fine for a
throwaway preview and wrong for anything you care about.

## 3. Deploy

Push to `main`. Vercel detects `requirements.txt` and `api/index.py`, installs
the driver, and routes every path to the WSGI app via `vercel.json`.

## 4. Verify

```
https://<your-app>.vercel.app/__health
```

Expected:

```json
{"ok": true, "storage": {"backend": "postgres", "host": "aws-0-....pooler.supabase.com:6543"}, "leagues": 0}
```

`"backend": "files"` means `DATABASE_URL` did not reach the function — check it
is set for the environment you deployed to, then redeploy. Environment variable
changes do **not** apply to existing deployments.

## 5. Load your league

A fresh database has no leagues. Either use the setup wizard in the browser, or
push your local league up:

```bash
DATABASE_URL='postgresql://...' python3 - <<'PY'
import json, pathlib
from ff import storage, config as C
storage.reset()
cfg = C.migrate(json.loads(pathlib.Path("leagues/keg-south.json").read_text()))
C.save(cfg)
backend = storage.get_backend()
backend.save_projection_set(
    "sample_projections.csv",
    pathlib.Path("data/projections/sample_projections.csv").read_text(),
)
print("loaded:", [l["id"] for l in C.list_leagues()])
PY
```

That reads your local files and writes them to the hosted database. Run it from
a checkout with `psycopg2-binary` installed (`pip install psycopg2-binary`).

---

## Running the hosted stack locally

Useful for debugging the exact code path Vercel runs:

```bash
pip install psycopg2-binary
export DATABASE_URL='postgresql://...'
export APP_PASSWORD='something'
python3 -c "
from wsgiref.simple_server import make_server
from ff.wsgi import app
print('http://127.0.0.1:8000')
make_server('127.0.0.1', 8000, app).serve_forever()"
```

## Tests against Postgres

The storage tests run against both backends and assert identical behaviour.
Postgres runs are skipped unless you point them at a database:

```bash
export FF_TEST_DATABASE_URL='postgresql://...'
python3 -m unittest discover -s tests
```

**Use a throwaway database.** The suite truncates every table between tests.

## Known limits of this deployment

- **One shared password, not user accounts.** Everyone with the password sees
  and edits the same leagues.
- **Cold starts.** The first request after idle takes a second or two while the
  function boots and connects. Fine for setup and weekly management; if it ever
  matters during a live draft, run locally that night — the same code, with no
  network in the path at all.
- **A connection per invocation.** Handled by the Supabase transaction pooler;
  do not use the direct `5432` connection string.
