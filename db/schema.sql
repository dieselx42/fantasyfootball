-- Multi-user schema for the fantasy assistant.
--
-- Target: Supabase (Postgres + Google sign-in + row level security).
-- Paste into the Supabase SQL editor, or run with psql against any Postgres
-- if you supply your own `auth.users` equivalent.
--
-- The design decision that matters: a league's rules stay a single JSON
-- document in `leagues.config`. That is the exact dict ff/config.py already
-- builds and validates, so the schema, defaults, validation and migrate()
-- all survive the move to a database untouched. Adding a scoring rule stays
-- a config change, not a migration.

-- ---------------------------------------------------------------- users

-- Supabase creates and manages auth.users via Google sign-in. We keep our own
-- profile row so the app never has to touch the auth schema directly.
create table if not exists profiles (
  id           uuid primary key references auth.users (id) on delete cascade,
  email        text,
  display_name text,
  created_at   timestamptz not null default now()
);

-- Populate a profile automatically on first sign-in.
create or replace function handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
  insert into profiles (id, email, display_name)
  values (
    new.id,
    new.email,
    coalesce(new.raw_user_meta_data ->> 'full_name', split_part(new.email, '@', 1))
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();

-- ---------------------------------------------------------------- leagues

create table if not exists leagues (
  id         uuid primary key default gen_random_uuid(),
  owner_id   uuid not null references profiles (id) on delete cascade,
  slug       text not null,                      -- "keg-south"
  name       text not null,
  season     integer not null,
  config     jsonb not null,                     -- the whole league config
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (owner_id, slug)
);

create index if not exists leagues_owner_idx on leagues (owner_id);

-- Membership is separate from ownership so league-mates can sign in later:
-- the commissioner edits the rules, everyone else reads them and manages the
-- one team they own.
create table if not exists league_members (
  league_id uuid not null references leagues (id) on delete cascade,
  user_id   uuid not null references profiles (id) on delete cascade,
  role      text not null default 'member'
              check (role in ('commissioner', 'member')),
  team_id   text,                                -- matches config -> teams[].id
  joined_at timestamptz not null default now(),
  primary key (league_id, user_id)
);

create index if not exists league_members_user_idx on league_members (user_id);

-- ---------------------------------------------------------------- state

create table if not exists picks (
  league_id     uuid not null references leagues (id) on delete cascade,
  overall       integer not null,
  round         integer not null,
  pick_in_round integer not null,
  team_id       text not null,
  player_id     text,
  player_name   text,
  pos           text,
  price         numeric,
  primary key (league_id, overall)
);

create table if not exists rosters (
  league_id uuid not null references leagues (id) on delete cascade,
  team_id   text not null,
  player_id text not null,
  acquired  text not null default 'draft',
  primary key (league_id, team_id, player_id)
);

create table if not exists saved_trades (
  id         bigserial primary key,
  league_id  uuid not null references leagues (id) on delete cascade,
  created_at timestamptz not null default now(),
  payload    jsonb not null
);

create table if not exists league_kv (
  league_id uuid not null references leagues (id) on delete cascade,
  key       text not null,
  value     jsonb not null,
  primary key (league_id, key)
);

-- ---------------------------------------------------------------- projections

-- Replaces the shared data/projections directory. Scoped to an owner, and
-- optionally to one league, so one person's upload cannot change anyone
-- else's valuations.
create table if not exists projection_sets (
  id          uuid primary key default gen_random_uuid(),
  owner_id    uuid not null references profiles (id) on delete cascade,
  league_id   uuid references leagues (id) on delete cascade,   -- null = all my leagues
  name        text not null,
  source      text,
  uploaded_at timestamptz not null default now(),
  players     jsonb not null            -- parsed rows, not the raw CSV
);

create index if not exists projection_sets_owner_idx on projection_sets (owner_id);

-- ---------------------------------------------------------------- platform tokens

-- Fixes the worst single-tenant bug: one Yahoo token file per process meant a
-- second user's sign-in overwrote the first's credentials. One row per user
-- per platform instead.
--
-- Encrypt these at rest. In Supabase use Vault; otherwise pgcrypto with a key
-- held in an environment variable the database never sees.
create table if not exists platform_tokens (
  user_id       uuid not null references profiles (id) on delete cascade,
  platform      text not null,                   -- 'yahoo' | 'espn' | 'sleeper'
  access_token  text not null,
  refresh_token text,
  obtained_at   timestamptz not null default now(),
  expires_in    integer,
  primary key (user_id, platform)
);

-- ---------------------------------------------------------------- row level security
--
-- This is the reason to prefer Supabase over a bare Postgres. Without RLS,
-- every query in the app has to remember `where owner_id = $me`, and forgetting
-- once exposes another user's league. With RLS the database refuses regardless
-- of what the application code does.

alter table profiles         enable row level security;
alter table leagues          enable row level security;
alter table league_members   enable row level security;
alter table picks            enable row level security;
alter table rosters          enable row level security;
alter table saved_trades     enable row level security;
alter table league_kv        enable row level security;
alter table projection_sets  enable row level security;
alter table platform_tokens  enable row level security;

create policy "own profile" on profiles
  for all using (id = auth.uid()) with check (id = auth.uid());

-- Readable if you own the league or belong to it.
create policy "read my leagues" on leagues
  for select using (
    owner_id = auth.uid()
    or exists (
      select 1 from league_members m
      where m.league_id = leagues.id and m.user_id = auth.uid()
    )
  );

-- Only the owner changes the rules.
create policy "owner writes league" on leagues
  for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

create policy "read my memberships" on league_members
  for select using (
    user_id = auth.uid()
    or exists (
      select 1 from leagues l
      where l.id = league_members.league_id and l.owner_id = auth.uid()
    )
  );

create policy "owner manages members" on league_members
  for all using (
    exists (
      select 1 from leagues l
      where l.id = league_members.league_id and l.owner_id = auth.uid()
    )
  );

-- Draft state, rosters, trades and kv all inherit access from their league.
create or replace function can_access_league(target uuid)
returns boolean
language sql stable security definer set search_path = public
as $$
  select exists (
    select 1 from leagues l
    where l.id = target
      and (
        l.owner_id = auth.uid()
        or exists (
          select 1 from league_members m
          where m.league_id = l.id and m.user_id = auth.uid()
        )
      )
  );
$$;

create policy "league scoped" on picks
  for all using (can_access_league(league_id)) with check (can_access_league(league_id));
create policy "league scoped" on rosters
  for all using (can_access_league(league_id)) with check (can_access_league(league_id));
create policy "league scoped" on saved_trades
  for all using (can_access_league(league_id)) with check (can_access_league(league_id));
create policy "league scoped" on league_kv
  for all using (can_access_league(league_id)) with check (can_access_league(league_id));

create policy "own projections" on projection_sets
  for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

-- Tokens are never readable by anyone but their owner.
create policy "own tokens" on platform_tokens
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

-- ---------------------------------------------------------------- housekeeping

create or replace function touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists leagues_touch on leagues;
create trigger leagues_touch before update on leagues
  for each row execute function touch_updated_at();
