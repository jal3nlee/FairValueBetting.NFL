-- sql/player_props_schema.sql
-- NFL player-prop storage (Phase 1). Mirrors the odds_snapshots/odds_lines
-- append-only snapshot pattern already used for Moneyline/Spread/Total
-- (core/data_sources.py), but scoped per event: each snapshot covers
-- exactly one event_id + prop market, since props are fetched one event
-- at a time via The Odds API's event-specific endpoint
-- (fetch_odds_nfl_props.py), rather than one bulk per-sport pull.
--
-- This file is the authoritative schema for three tables already read
-- and written by existing, committed code — it adds no columns beyond
-- what that code actually uses:
--   - writes:  fetch_odds_nfl_props.py
--   - reads:   core/nfl_prop_data_sources.py
--   - consumes: core/nfl_prop_pipeline.py (via the reads above)
--
-- This repo has no migration tooling (no migrations/ dir, no ORM) --
-- odds_snapshots/odds_lines aren't defined here either, only referenced
-- by column name. Run this manually against production Supabase (SQL
-- editor, or `supabase db execute -f sql/player_props_schema.sql`).
-- `create table if not exists` makes this safe to (re)run, but it will
-- NOT alter an existing table of the same name that has a different
-- shape -- if these tables already exist from an earlier manual setup,
-- diff their columns against this file by hand before relying on it.
--
-- RLS: apply the same policy already used for odds_snapshots/odds_lines
-- (reads via SUPABASE_ANON_KEY, writes via the service-role
-- SUPABASE_KEY -- see CLAUDE.md's "two distinct Supabase keys" note).
-- That policy isn't defined anywhere in this repository, so it isn't
-- reproduced here -- apply it directly in the Supabase dashboard/SQL
-- editor to match whatever odds_snapshots/odds_lines already have.
--
-- Nothing here is destructive: no DROP, no data-modifying statement.

create table if not exists player_prop_snapshots (
    id            uuid primary key,
    event_id      text not null,
    market        text not null,
    pulled_at     timestamptz not null,
    home_team     text,
    away_team     text,
    commence_time timestamptz not null
);

-- get_latest_prop_snapshot_meta(): .eq(event_id).eq(market).order(pulled_at desc).limit(1)
create index if not exists idx_player_prop_snapshots_event_market_pulled
    on player_prop_snapshots (event_id, market, pulled_at desc);

-- fetch_odds_nfl_props.py::_get_last_prop_pull_time(): .eq(event_id).order(pulled_at desc).limit(1)
create index if not exists idx_player_prop_snapshots_event_pulled
    on player_prop_snapshots (event_id, pulled_at desc);

-- get_upcoming_prop_event_ids(): .gte(commence_time, start).lte(commence_time, end)
create index if not exists idx_player_prop_snapshots_commence
    on player_prop_snapshots (commence_time);


create table if not exists player_prop_lines (
    snapshot_id    uuid not null references player_prop_snapshots (id) on delete cascade,
    event_id       text not null,
    home_team      text,
    away_team      text,
    commence_time  timestamptz not null,
    book           text not null,
    market         text not null,
    side           text not null,
    line           numeric,
    price          integer,
    player_key     text not null,
    player_display text not null
);

-- get_prop_lines_for_snapshot(): .eq(snapshot_id).range(...) (paginated)
create index if not exists idx_player_prop_lines_snapshot
    on player_prop_lines (snapshot_id);


create table if not exists player_prop_fetch_log (
    id                uuid primary key,
    run_at            timestamptz not null,
    event_id          text,
    markets_requested integer,
    markets_returned  integer,
    estimated_credits integer
);

-- No in-app read path queries this table today (write-only from
-- fetch_odds_nfl_props.py); this index only supports ad hoc reporting
-- (e.g. "credits used per event over time") via the Supabase SQL editor.
create index if not exists idx_player_prop_fetch_log_event_run
    on player_prop_fetch_log (event_id, run_at desc);
