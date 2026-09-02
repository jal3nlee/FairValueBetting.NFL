# core/nfl_prop_data_sources.py
# Supabase reads for the dedicated player-prop odds tables — mirrors
# core/data_sources.py::fetch_market_lines's exact shape (latest
# snapshot per key, then that snapshot's lines), but scoped by
# event_id + prop market key instead of sport + game-market key, and
# reading from player_prop_snapshots / player_prop_lines rather than
# odds_snapshots / odds_lines. The existing game-market tables and
# core/data_sources.py are not imported or modified here — this module
# is fully additive and isolated.
import pandas as pd
import streamlit as st

PAGE_SIZE = 1000


@st.cache_data(ttl=300, show_spinner=False)
def get_latest_prop_snapshot_meta(_supabase, event_id: str, prop_market: str):
    try:
        res = (
            _supabase.table("player_prop_snapshots")
            .select("id,pulled_at")
            .eq("event_id", event_id)
            .eq("market", prop_market)
            .order("pulled_at", desc=True)
            .limit(1)
            .execute()
        )
        data = res.data or []
        if not data:
            return None, None
        row = data[0]
        return row["id"], row.get("pulled_at")
    except Exception:
        return None, None


@st.cache_data(ttl=300, show_spinner=False)
def get_prop_lines_for_snapshot(_supabase, snapshot_id: str) -> pd.DataFrame:
    rows, start = [], 0
    while True:
        page = (
            _supabase.table("player_prop_lines")
            .select(
                "event_id,home_team,away_team,commence_time,book,market,side,line,"
                "price,player_key,player_display"
            )
            .eq("snapshot_id", snapshot_id)
            .range(start, start + PAGE_SIZE - 1)
            .execute()
        )
        chunk = page.data or []
        rows.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return pd.DataFrame(rows)


def fetch_prop_market_lines(_supabase, event_ids: list, prop_market: str) -> pd.DataFrame:
    """
    Returns the latest stored lines for one prop market across the given
    events — analogous to core/data_sources.py::fetch_market_lines, but
    per-event rather than per-sport-key, since prop snapshots are
    naturally scoped to a single event (see fetch_odds_nfl_props.py).
    """
    all_lines = []
    for event_id in sorted(set(event_ids)):
        snap_id, _pulled_at = get_latest_prop_snapshot_meta(_supabase, event_id, prop_market)
        if not snap_id:
            continue
        df = get_prop_lines_for_snapshot(_supabase, snap_id)
        if not df.empty:
            all_lines.append(df)
    if all_lines:
        return pd.concat(all_lines, ignore_index=True)
    return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def get_upcoming_prop_event_ids(_supabase, window_start_iso: str, window_end_iso: str) -> list:
    """
    Distinct event_ids with at least one stored prop snapshot whose
    commence_time falls in the given window — used by the Fair Value
    Model's Player Props view to know which events to query per market,
    without needing a separate "NFL schedule" data source. Read-only;
    does not trigger any fetch.
    """
    try:
        res = (
            _supabase.table("player_prop_snapshots")
            .select("event_id,commence_time")
            .gte("commence_time", window_start_iso)
            .lte("commence_time", window_end_iso)
            .execute()
        )
        rows = res.data or []
        return sorted({r["event_id"] for r in rows if r.get("event_id")})
    except Exception:
        return []
