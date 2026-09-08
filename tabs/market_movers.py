# tabs/market_movers.py
import pandas as pd
import streamlit as st

from core.odds_math import parse_iso_dt_utc, EASTERN, fmt_odds
from core.pipeline import MARKETS, run_market_pipeline
from core.data_sources import fetch_market_lines, filter_by_window, get_date_window, infer_current_week_index
from core.nfl_prop_market_config import PROP_MARKETS
from core.nfl_prop_pipeline import run_prop_market_pipeline
from core.nfl_prop_data_sources import fetch_prop_market_lines, get_upcoming_prop_event_ids


def _sc_name(book: str) -> str:
    _display = {
        "eu_pinnacle": "Pinnacle", "pinnacle": "Pinnacle", "fanduel": "FanDuel",
        "draftkings": "DraftKings", "caesars": "Caesars", "bet365": "bet365", "kalshi": "Kalshi",
        "betmgm": "BetMGM", "espnbet": "ESPN Bet", "fanatics": "Fanatics",
        "hardrockbet": "Hard Rock Bet", "betrivers": "BetRivers", "ballybet": "Bally Bet",
        "betparx": "betParx", "betonline": "BetOnline", "lowvig": "LowVig", "fliff": "Fliff",
        "rebet": "Rebet", "betanysports": "BetAnySports", "bovada": "Bovada",
        "mybookie": "MyBookie", "betus": "BetUS", "underdog": "Underdog", "prophetx": "ProphetX",
    }
    return _display.get(str(book).lower(), str(book).replace("_", " ").title())


# Same team-abbreviation map and ESPN CDN logo URL pattern already used by
# Sportsbook Screener / Matchup Center / Parlay Builder — reused as-is rather
# than introducing a second logo dataset.
NFL_TEAM_ABBR = {
    "Arizona Cardinals": "ari", "Atlanta Falcons": "atl", "Baltimore Ravens": "bal",
    "Buffalo Bills": "buf", "Carolina Panthers": "car", "Chicago Bears": "chi",
    "Cincinnati Bengals": "cin", "Cleveland Browns": "cle", "Dallas Cowboys": "dal",
    "Denver Broncos": "den", "Detroit Lions": "det", "Green Bay Packers": "gb",
    "Houston Texans": "hou", "Indianapolis Colts": "ind", "Jacksonville Jaguars": "jax",
    "Kansas City Chiefs": "kc", "Las Vegas Raiders": "lv", "Los Angeles Chargers": "lac",
    "Los Angeles Rams": "lar", "Miami Dolphins": "mia", "Minnesota Vikings": "min",
    "New England Patriots": "ne", "New Orleans Saints": "no", "New York Giants": "nyg",
    "New York Jets": "nyj", "Philadelphia Eagles": "phi", "Pittsburgh Steelers": "pit",
    "San Francisco 49ers": "sf", "Seattle Seahawks": "sea", "Tampa Bay Buccaneers": "tb",
    "Tennessee Titans": "ten", "Washington Commanders": "wsh",
}


def _logo_url(team_name: str) -> str | None:
    abbr = NFL_TEAM_ABBR.get(team_name)
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png" if abbr else None


def _mm_fair_odds_str(row, is_prop: bool) -> str:
    """Fair odds for this row's own Pick/Side, matching the same
    pick_is_a selection already used by tabs/fair_value_model.py's
    _recompute_row/_recompute_prop_row — mi_fair_odds_a/mi_fair_odds_b are
    attached at the game/group level by build_market_intelligence
    (unmodified, protected), not recalculated here."""
    if is_prop:
        pick_is_a = row.get("Side") == "Over"
    else:
        pick_label = row.get("Pick", "")
        game = row.get("Game", "")
        home_team = game.split(" vs ")[0] if isinstance(game, str) and " vs " in game else None
        pick_is_a = pick_label == "Over" if pick_label in ("Over", "Under") else pick_label == home_team
    fair_val = row.get("mi_fair_odds_a") if pick_is_a else row.get("mi_fair_odds_b")
    return fmt_odds(int(fair_val)) if fair_val is not None and pd.notna(fair_val) else "—"


def _mm_prop_bet_label(row) -> str:
    """Mirrors tabs/fair_value_model.py::_prop_bet_label — duplicated
    locally rather than cross-imported, matching this codebase's existing
    per-tab-private-helper convention (see _sc_name above, and CLAUDE.md's
    note on the duplicated sportsbook-display-name pattern)."""
    player = row.get("Player", "—")
    side = row.get("Side", "")
    line = row.get("Line")
    has_line = pd.notna(line) and str(line).strip()
    market = row.get("Market", "")
    line_part = f" {line}" if has_line else ""
    return f"{player} {side}{line_part} {market}".replace("  ", " ").strip()


def render(supabase, now_utc, eff_bankroll, eff_kelly):
    # ── This-week data — independent of the Fair Value Model's Date Range.
    # NFL games cluster on Thu/Sun/Mon, so a literal "Today" window (the
    # MLB pattern this was ported from) shows empty on most days. Use the
    # current NFL week instead.
    _current_week = infer_current_week_index(now_utc)
    _week_label = "NFL Preseason" if _current_week == 0 else f"NFL Week {_current_week}"
    _week_start, _week_end, _sport_keys, _week_caption = get_date_window(now_utc, _week_label)
    _week_raw: dict[str, pd.DataFrame] = {}
    _week_display: dict[str, pd.DataFrame] = {}
    _week_pulled: list = []

    for _mkt_key, _mkt_cfg in MARKETS.items():
        _raw_t, _pulled_t = fetch_market_lines(supabase, _sport_keys, _mkt_cfg.db_market_key)
        _raw_t_filtered = filter_by_window(_raw_t, _week_start, _week_end)
        _week_raw[_mkt_key] = _raw_t_filtered
        _week_pulled.extend(_pulled_t)
        _week_display[_mkt_key] = run_market_pipeline(
            raw_lines=_raw_t_filtered, cfg=_mkt_cfg, bankroll=eff_bankroll, kelly=eff_kelly,
            min_ev=0.0, min_fair_pct=0.0, show_all=True,
        )

    df_week = (
        pd.concat([df for df in _week_display.values() if not df.empty], ignore_index=True)
        if any(not df.empty for df in _week_display.values())
        else pd.DataFrame()
    )

    # ── Player Props for the same week window — additive to Game Markets,
    # never a dependency: any failure here (no data yet, ingestion not run,
    # malformed rows) degrades to "no prop plays" without affecting Game
    # Markets' own Top EV Plays below. Calls the exact same
    # PROP_MARKETS/run_prop_market_pipeline path the Fair Value Model's
    # Player Props view uses (core/nfl_prop_pipeline.py, unmodified) — not
    # a second, independent EV calculation.
    df_props_week = pd.DataFrame()
    try:
        _prop_event_ids = get_upcoming_prop_event_ids(supabase, _week_start.isoformat(), _week_end.isoformat())
        _prop_frames = []
        for _pmkt_key, _pmkt_cfg in PROP_MARKETS.items():
            if not _prop_event_ids:
                break
            _praw = fetch_prop_market_lines(supabase, _prop_event_ids, _pmkt_key)
            _praw_filtered = filter_by_window(_praw, _week_start, _week_end)
            _pdf = run_prop_market_pipeline(
                raw_lines=_praw_filtered, cfg=_pmkt_cfg, bankroll=eff_bankroll, kelly=eff_kelly,
                min_ev=0.0, min_fair_pct=0.0, show_all=True,
            )
            if not _pdf.empty:
                _prop_frames.append(_pdf)
        if _prop_frames:
            df_props_week = pd.concat(_prop_frames, ignore_index=True)
    except Exception:
        df_props_week = pd.DataFrame()

    with st.expander("Market Movers", expanded=True):
        st.caption(
            "A high-level view of this week's NFL betting market. "
            "Highlights the most significant activity across games, markets, and sportsbooks."
        )

        # ── Market Snapshot ──────────────────────────────────────
        st.markdown(f"**{_week_label} Market Snapshot**")

        _snap_games = 0
        _snap_books = 0
        _snap_markets = 0
        _latest_pull = None

        for _mkt_key, _raw in _week_raw.items():
            if not _raw.empty:
                _snap_games = max(_snap_games, _raw[["home_team", "away_team"]].drop_duplicates().shape[0])
                _snap_books = max(_snap_books, _raw["book"].nunique())
                _snap_markets += 1

        for _p in _week_pulled:
            _dt = parse_iso_dt_utc(_p)
            if _dt and (_latest_pull is None or _dt > _latest_pull):
                _latest_pull = _dt

        _pull_str = (
            _latest_pull.astimezone(EASTERN).strftime("%b %d  %I:%M %p ET") if _latest_pull else "—"
        )

        _snapshot_rows = [
            {"Metric": "Games This Week", "Value": str(_snap_games) if _snap_games else "—"},
            {"Metric": "Active Markets", "Value": str(_snap_markets) if _snap_markets else "—"},
            {"Metric": "Sportsbooks", "Value": str(_snap_books) if _snap_books else "—"},
            {"Metric": "Odds Last Updated", "Value": _pull_str},
        ]
        st.dataframe(
            pd.DataFrame(_snapshot_rows), use_container_width=True, hide_index=True,
            height=38 + 35 * len(_snapshot_rows),
        )

        st.divider()

        # ── Top EV Plays ─────────────────────────────────────
        # Combines Game Markets and Player Props into one ranked list —
        # both are read from their own existing, protected pipelines
        # (run_market_pipeline / run_prop_market_pipeline) and only
        # normalized here into a common minimal shape for ranking/display;
        # no EV/Fair Odds is recalculated independently for either.
        st.markdown("**This Week's Top EV Plays**")
        st.caption(
            "The three highest expected value betting opportunities identified by "
            f"the Fair Value Model for {_week_label.lower()}, across game markets and player props."
        )

        _combined_plays = []

        if not df_week.empty:
            _ev_col = pd.to_numeric(df_week["EV%"].astype(str).str.replace("%", "", regex=False), errors="coerce")
            for _, _r in df_week.assign(_ev_num=_ev_col).loc[lambda d: d["_ev_num"] > 0].iterrows():
                _combined_plays.append({
                    "_ev_num": _r["_ev_num"], "_is_prop": False,
                    "Game": _r.get("Game", "—"), "Market": _r.get("Market", "—"), "Pick": _r.get("Pick", "—"),
                    "Best Book": _r.get("Best Book", "—"),
                    "Best Odds (Fair)": f"{_r.get('Best Odds', '—')} ({_mm_fair_odds_str(_r, False)})",
                    "EV%": _r.get("EV%", "—"),
                })

        if not df_props_week.empty:
            _pev_col = pd.to_numeric(df_props_week["EV%"].astype(str).str.replace("%", "", regex=False), errors="coerce")
            for _, _r in df_props_week.assign(_ev_num=_pev_col).loc[lambda d: d["_ev_num"] > 0].iterrows():
                _combined_plays.append({
                    "_ev_num": _r["_ev_num"], "_is_prop": True,
                    "Game": _r.get("Game", "—"), "Bet": _mm_prop_bet_label(_r),
                    "Best Book": _r.get("Best Book", "—"),
                    "Best Odds (Fair)": f"{_r.get('Best Odds', '—')} ({_mm_fair_odds_str(_r, True)})",
                    "EV%": _r.get("EV%", "—"),
                })

        # Rank descending across the combined pool — no reserved slots per
        # category, matching how this section already ranked game markets
        # alone before props were added.
        _top_ev = sorted(_combined_plays, key=lambda p: p["_ev_num"], reverse=True)[:3]

        if not _top_ev:
            st.info("No positive EV opportunities were identified at this time.")
        else:
            for _p in _top_ev:
                _game = _p.get("Game", "—")
                _teams = _game.split(" vs ") if isinstance(_game, str) else []
                _home_team = _teams[0] if len(_teams) == 2 else None
                _away_team = _teams[1] if len(_teams) == 2 else None
                _away_logo = _logo_url(_away_team) if _away_team else None
                _home_logo = _logo_url(_home_team) if _home_team else None

                with st.container(border=True):
                    if _away_logo and _home_logo:
                        st.markdown(
                            f"<img src='{_away_logo}' width='22' style='vertical-align:middle;margin-right:5px'/>"
                            f"**{_away_team}** @ "
                            f"<img src='{_home_logo}' width='22' style='vertical-align:middle;margin:0 5px'/>"
                            f"**{_home_team}**",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(f"**{_game}**")

                    if _p["_is_prop"]:
                        st.caption(_p.get("Bet", "—"))
                    else:
                        st.caption(f"{_p.get('Market', '—')} · Pick: {_p.get('Pick', '—')}")

                    st.dataframe(
                        pd.DataFrame([{
                            "Best Book": _sc_name(_p.get("Best Book", "—")),
                            "Best Odds": _p.get("Best Odds (Fair)", "—"),
                            "EV%": _p.get("EV%", "—"),
                        }]),
                        use_container_width=True, hide_index=True, height=38 + 35,
                    )

            st.caption("See the full list of positive EV opportunities below.")

        st.divider()
        st.caption("Use **Matchup Center** to research a specific game.")

    st.divider()
