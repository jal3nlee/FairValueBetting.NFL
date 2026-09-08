# core/data_sources.py
# Supabase odds readers and NFL date-window helpers.
# Functions take the Supabase client as a leading-underscore parameter
# so Streamlit's @st.cache_data doesn't try to hash an unhashable client.
from datetime import datetime, timezone, timedelta
import pandas as pd
import streamlit as st

from core.odds_math import EASTERN, parse_iso_dt_utc

PAGE_SIZE = 1000

MARKET_MAP = {
    "moneyline": "h2h",
    "spread":    "spreads",
    "total":     "totals",
}


@st.cache_data(ttl=300, show_spinner=False)
def get_latest_snapshot_meta(_supabase, sport: str, market: str, region: str = "us"):
    try:
        res = (
            _supabase.table("odds_snapshots")
            .select("id,pulled_at")
            .eq("sport", sport)
            .eq("market", market)
            .eq("region", region)
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
def get_lines_for_snapshot(_supabase, snapshot_id: str):
    rows, start = [], 0
    while True:
        page = (
            _supabase.table("odds_lines")
            .select("event_id,home_team,away_team,commence_time,book,market,side,line,price")
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


def fetch_market_lines(_supabase, sport_keys: set, market_label: str):
    all_lines, pulled_ats = [], []
    db_market = MARKET_MAP.get(market_label, market_label)
    for sport in sorted(sport_keys):
        snap_id, pulled_at = get_latest_snapshot_meta(_supabase, sport, db_market, region="us")
        if not snap_id:
            continue
        df = get_lines_for_snapshot(_supabase, snap_id)
        if not df.empty:
            all_lines.append(df)
        if pulled_at:
            pulled_ats.append(pulled_at)
    if all_lines:
        return pd.concat(all_lines, ignore_index=True), pulled_ats
    return pd.DataFrame(), pulled_ats


def filter_by_window(df: pd.DataFrame, window_start, window_end) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["__t0"] = df["commence_time"].apply(parse_iso_dt_utc)
    df = df[(df["__t0"] >= window_start) & (df["__t0"] <= window_end)]
    return df.drop(columns=["__t0"])


# =======================
# NFL WEEK / DATE WINDOW
# =======================
def thursday_after_labor_day_utc(year: int) -> datetime:
    """Thursday after Labor Day at 00:00 ET, converted to UTC."""
    d = datetime(year, 9, 1, tzinfo=EASTERN)
    while d.weekday() != 0:  # 0 = Monday
        d += timedelta(days=1)
    opener_et = (d + timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    return opener_et.astimezone(timezone.utc)


def nfl_week_window_utc(week_index: int, now_utc: datetime):
    """Returns (start_utc, end_utc) for the given week index."""
    yr = now_utc.astimezone(EASTERN).year
    start = thursday_after_labor_day_utc(yr) + timedelta(days=7 * (week_index - 1))
    end = start + timedelta(days=5, hours=23, minutes=59, seconds=59)
    return start, end


def infer_current_week_index(now_utc: datetime) -> int:
    """Return 1 before Week 1 has started (explicitly surfaces Week 1
    rather than a separate preseason window); otherwise clamp to 1..18."""
    yr = now_utc.astimezone(EASTERN).year
    wk1 = thursday_after_labor_day_utc(yr)
    if now_utc < wk1:
        return 1
    weeks = (now_utc - wk1).days // 7 + 1
    return max(1, min(18, weeks))


def sport_key_for_week(week_index: int) -> str:
    return "NFL"


def window_next_7_days(now_utc: datetime, tz=EASTERN):
    local = now_utc.astimezone(tz)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = (start_local + timedelta(days=8)).replace(hour=23, minute=59, second=59, microsecond=0)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def short_day_md(dt_utc: datetime) -> str:
    local = dt_utc.astimezone(EASTERN)
    return f"{local.strftime('%a')} {local.month}/{local.day}"


def get_date_window(now_utc: datetime, window_choice: str):
    """
    Resolves the Date Range selector's choice into (start, end, sport_keys, caption_label).
    window_choice is one of: "Today", "<This Week label>", "Next 7 Days".
    """
    current_week = infer_current_week_index(now_utc)
    week_label = f"NFL Week {current_week}"

    if window_choice == "Today":
        now_local = datetime.now(EASTERN)
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        end = (start + timedelta(days=1)) - timedelta(seconds=1)
        sport_keys = {sport_key_for_week(current_week)}
        return start, end, sport_keys, "Today"

    if window_choice == "Next 7 Days":
        start, end = window_next_7_days(now_utc, tz=EASTERN)
        sport_keys = {sport_key_for_week(infer_current_week_index(start)), sport_key_for_week(infer_current_week_index(end))}
        return start, end, sport_keys, f"{short_day_md(start)} – {short_day_md(end)}"

    # "This Week" / week_label
    start, end = nfl_week_window_utc(current_week, now_utc)
    sport_keys = {sport_key_for_week(current_week)}
    return start, end, sport_keys, f"{week_label} — {short_day_md(start)} – {short_day_md(end)}"
