# tabs/matchup_center.py
import pandas as pd
import streamlit as st

from core.odds_math import american_to_decimal, american_to_implied_prob, fmt_odds, parse_iso_dt_utc, EASTERN
from core.pipeline import MARKETS, run_market_pipeline
from core.data_sources import fetch_market_lines, filter_by_window, get_date_window, infer_current_week_index
from core.nfl_live_scores import fetch_nfl_scoreboard, get_game_status, format_live_line
from core.nflverse_data import get_current_season
from core.nfl_matchup_context import (
    TEAM_COMPARISON_METRICS, get_team_comparison_with_fallback, get_team_recent_form_with_fallback,
)
from core.nfl_injuries_data import get_team_injuries

TIPS = {
    "ev":        "Expected Value — your edge vs. the fair price. Positive = favorable bet.",
    "best_odds": "Best available odds — the best price found across all sportsbooks.",
}

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


def _logo_url(team_name: str) -> str | None:
    abbr = NFL_TEAM_ABBR.get(team_name)
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png" if abbr else None


def _to_int_odds(x):
    try:
        return int(str(x).replace("+", ""))
    except Exception:
        return None


def _fmt_odds_in_format(american_price, fmt: str) -> str:
    """Same small display-only formatting pattern already used in
    tabs/fair_value_model.py::_fmt_best_odds / tabs/parlay_builder.py — no
    new conversion methodology, just American/Decimal/Implied % display
    of an already-computed American price."""
    if american_price is None:
        return "—"
    if fmt == "American":
        return fmt_odds(american_price)
    elif fmt == "Decimal":
        return f"{american_to_decimal(american_price):.2f}x"
    else:
        p = american_to_implied_prob(american_price)
        return f"{p * 100:.1f}%" if p else "—"


def _bet_label(row) -> str:
    """Explicit selected-side label, e.g. 'Arizona Cardinals ML',
    'Arizona Cardinals -2.5', 'Over 44.5' — carries its own signed line
    so the user never has to infer which side owns it. row["Line"] is
    already the selected pick's own correctly-signed value
    (core/pipeline.py::build_display_rows pairs each row's "Line" with
    that row's own Pick), so no extra sign derivation is needed here."""
    market = row.get("Market", "")
    pick = row.get("Pick", "—")
    line = row.get("Line")
    if market == "Moneyline":
        return f"{pick} ML"
    if pd.notna(line) and str(line) not in ("", "nan"):
        return f"{pick} {line}"
    return pick


def _build_snap_rows(source: pd.DataFrame, odds_format: str) -> list[dict]:
    rows = []
    for _, r in source.iterrows():
        rows.append({
            "Bet":       _bet_label(r),
            "Best Book": _sc_name(r.get("Best Book", "—")),
            "Best Odds": _fmt_odds_in_format(_to_int_odds(r.get("Best Odds")), odds_format),
            "EV%":       r.get("EV%", "—"),
        })
    return rows


@st.cache_data(ttl=300, show_spinner=False)
def _load_matchup_center_markets(_supabase, sport_keys: frozenset, window_start, window_end, bankroll: float, kelly: float) -> dict[str, pd.DataFrame]:
    """
    Pure computation: fetch + price all 3 game markets for Matchup
    Center. st.tabs() re-executes every tab's render() on every app-wide
    widget interaction, and this tab has no auth gate, so without this
    the full 3-market devig/consensus/EV pipeline re-ran on every
    unrelated click anywhere in the app (e.g. a Prop Research dropdown)
    even though the underlying Supabase reads are already cached. Same
    pattern as tabs/market_movers.py::_load_market_movers_data. Cache key
    uses window_start/window_end (already resolved from the user's Date
    Range choice) rather than raw now_utc.
    """
    _display: dict[str, pd.DataFrame] = {}
    for _mkt_key, _mkt_cfg in MARKETS.items():
        _raw_mc, _pulled = fetch_market_lines(_supabase, sport_keys, _mkt_cfg.db_market_key)
        _raw_mc = filter_by_window(_raw_mc, window_start, window_end)
        _display[_mkt_key] = run_market_pipeline(
            raw_lines=_raw_mc, cfg=_mkt_cfg, bankroll=bankroll, kelly=kelly,
            min_ev=0.0, min_fair_pct=0.0, show_all=True,
        )
    return _display


def render(supabase, now_utc, eff_bankroll, eff_kelly):
    st.markdown("## Matchup Center")

    _wk = infer_current_week_index(now_utc)
    _week_label = "NFL Preseason" if _wk == 0 else f"NFL Week {_wk}"
    _mc_col1, _mc_col2 = st.columns(2)
    with _mc_col1:
        _window_choice = st.selectbox(
            "Date Range", ["Today", _week_label, "Next 7 Days"], index=1, key="mc_window_choice",
        )
    with _mc_col2:
        _odds_format = st.selectbox(
            "Odds Format", ["American", "Decimal", "Implied %"], key="mc_odds_format",
        )
    window_start, window_end, sport_keys, caption_label = get_date_window(now_utc, _window_choice)

    st.caption(
        f"Every game for {caption_label}, grouped by start time. "
        "Click a matchup to open the full research breakdown."
    )

    _mc_display = _load_matchup_center_markets(
        supabase, frozenset(sport_keys), window_start, window_end, eff_bankroll, eff_kelly,
    )
    _df_mc = (
        pd.concat([df for df in _mc_display.values() if not df.empty], ignore_index=True)
        if any(not df.empty for df in _mc_display.values())
        else pd.DataFrame()
    )

    if _df_mc.empty:
        st.info(f"No games available for {caption_label}.")
        return

    # ── One shared scoreboard fetch for this whole render — reused per
    # game below, not re-fetched per card. ────────────────────────────
    _scoreboard_events = fetch_nfl_scoreboard()

    # Resolved once per render, not per team/game, and threaded through
    # explicitly to the Team Comparison/Recent Form/Injuries lookups
    # below (same reasoning as the shared scoreboard fetch above) — those
    # lookups are cached nflreadpy reads, but get_current_season() itself
    # currently has a debug st.caption side effect that would otherwise
    # fire dozens of times in one render if resolved per-team/per-game.
    _nfl_season = get_current_season()

    _games_ordered = (
        _df_mc.assign(_sort_dt=_df_mc["commence_time"].apply(parse_iso_dt_utc))
        .dropna(subset=["_sort_dt"])
        [["Game", "commence_time", "_sort_dt"]]
        .drop_duplicates(subset=["Game", "commence_time"])
        .sort_values("_sort_dt")
        [["Game", "commence_time"]]
        .to_records(index=False)
        .tolist()
    )

    _last_bucket = None
    for _game_label, _commence_iso in _games_ordered:
        _game_rows = _df_mc[
            (_df_mc["Game"] == _game_label) & (_df_mc["commence_time"] == _commence_iso)
        ].reset_index(drop=True)
        if _game_rows.empty:
            continue
        _dt = parse_iso_dt_utc(_commence_iso)
        if not _dt:
            continue
        _et = _dt.astimezone(EASTERN)
        _bucket = _et.replace(minute=0, second=0, microsecond=0)

        if _bucket != _last_bucket:
            st.markdown(f"##### {_bucket.strftime('%a %I:%M %p ET').lstrip('0').replace(' 0', ' ')}")
            _last_bucket = _bucket

        _teams = _game_label.split(" vs ")
        _ht = _teams[0] if len(_teams) == 2 else _game_label
        _at = _teams[1] if len(_teams) == 2 else ""

        # ── Live/Final status — pregame falls back to kickoff time ──
        _ht_abbr = NFL_TEAM_ABBR.get(_ht, "").upper()
        _at_abbr = NFL_TEAM_ABBR.get(_at, "").upper()
        _game_status = get_game_status(_scoreboard_events, _ht_abbr, _at_abbr)
        _live_line = format_live_line(_game_status)

        _favorite_str = _underdog_str = "—"
        _ml_rows = _game_rows[_game_rows["Market"] == "Moneyline"].copy()
        if not _ml_rows.empty:
            _ml_rows["_odds_num"] = _ml_rows["Best Odds"].apply(_to_int_odds)
            _ml_rows = _ml_rows.dropna(subset=["_odds_num"])
            if not _ml_rows.empty:
                _fav_row = _ml_rows.loc[_ml_rows["_odds_num"].idxmin()]
                _dog_row = _ml_rows.loc[_ml_rows["_odds_num"].idxmax()]
                _favorite_str = f"{_fav_row['Pick']} {_fav_row['Best Odds']}"
                _underdog_str = f"{_dog_row['Pick']} {_dog_row['Best Odds']}"

        _total_str = "—"
        _tot_rows = _game_rows[(_game_rows["Market"] == "Total") & (_game_rows["Pick"] == "Over")]
        if not _tot_rows.empty:
            _tr = _tot_rows.iloc[0]
            _total_str = f"O/U {_tr.get('Line','—')} ({_tr.get('Best Odds','—')})"

        _sp_str = "—"
        _sp_rows = _game_rows[(_game_rows["Market"] == "Spread") & (_game_rows["Pick"] == _ht)]
        if not _sp_rows.empty:
            _sr = _sp_rows.iloc[0]
            _sp_str = f"{_ht} {_sr.get('Line','—')} ({_sr.get('Best Odds','—')})"

        _away_logo = _logo_url(_at)
        _home_logo = _logo_url(_ht)

        # ── Compact card ─────────────────────────────────────────
        with st.container(border=True):
            _cc1, _cc2 = st.columns([4, 1])
            with _cc1:
                if _away_logo and _home_logo:
                    st.markdown(
                        f"<img src='{_away_logo}' width='20' style='vertical-align:middle;margin-right:4px'/>"
                        f"**{_at}** @ "
                        f"<img src='{_home_logo}' width='20' style='vertical-align:middle;margin:0 4px'/>"
                        f"**{_ht}**",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(f"**{_at} @ {_ht}**")
            _is_live = _game_status.get("state") == "in"
            with _cc2:
                if not _is_live:
                    st.caption(_et.strftime("%a %I:%M %p ET").lstrip("0").replace(" 0", " "))

            if _live_line:
                # One compact status line — not a separate badge plus a
                # second repeating line — red only while actually live;
                # scheduled/final states are never styled red.
                if _is_live:
                    st.markdown(
                        f"<span style='color:#e74c3c;font-weight:600'>{_live_line}</span>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption(_live_line)

            st.caption(f"ML: {_favorite_str} / {_underdog_str}   ·   {_total_str}   ·   Spread: {_sp_str}")

            with st.expander("View full matchup research", expanded=False):
                # ── 1. Betting Snapshot ────────────────────────────
                # The fast question this answers: what is the bet, where
                # is the best price, and what is the edge? Deeper model
                # detail (Fair Odds, Fair Probability, Confidence/
                # Dispersion) belongs in Fair Value Model, not here — see
                # CLAUDE.md's Matchup Center product boundary.
                st.markdown("#### Betting Snapshot")
                _snap_source = _game_rows.copy()
                _snap_col_config = {
                    "EV%": st.column_config.TextColumn("EV%", help=TIPS["ev"]),
                    "Best Odds": st.column_config.TextColumn("Best Odds", help=TIPS["best_odds"]),
                }
                for _mkt_name in ["Moneyline", "Spread", "Total"]:
                    _mkt_group = _snap_source[_snap_source["Market"] == _mkt_name]
                    if _mkt_group.empty:
                        continue
                    st.caption(_mkt_name)
                    st.dataframe(
                        pd.DataFrame(_build_snap_rows(_mkt_group, _odds_format)),
                        use_container_width=True, hide_index=True, column_config=_snap_col_config,
                    )
                st.divider()

                # ── 2. Team Comparison ─────────────────────────────
                # Season-to-date, from nflreadpy team_stats/schedules —
                # see core/nfl_matchup_context.py for exactly which
                # fields are/aren't reliably available (no third-down or
                # red-zone efficiency data exists in this dataset, so
                # those metrics are intentionally not included).
                st.markdown("#### Team Comparison")
                # Falls back to season - 1 for BOTH teams together when
                # current-season stats aren't usable for one or both yet
                # (early season) -- see get_team_comparison_with_fallback.
                # The metric computation itself (get_team_comparison_stats)
                # is unchanged; this only decides which season to call it
                # with, and reverts to current-season data automatically
                # once it's available.
                _away_cmp, _home_cmp, _cmp_season = get_team_comparison_with_fallback(_nfl_season, _at, _ht)
                if _away_cmp and _home_cmp:
                    _cmp_rows = []
                    for _cmp_key, _cmp_label in TEAM_COMPARISON_METRICS:
                        _av = _away_cmp.get(_cmp_key)
                        _hv = _home_cmp.get(_cmp_key)
                        if _cmp_key == "turnover_margin":
                            _av_s = f"{_av:+d}" if _av is not None else "—"
                            _hv_s = f"{_hv:+d}" if _hv is not None else "—"
                        else:
                            _av_s = f"{_av:.1f}" if _av is not None else "—"
                            _hv_s = f"{_hv:.1f}" if _hv is not None else "—"
                        _cmp_rows.append({"Away": _av_s, "Metric": _cmp_label, "Home": _hv_s})
                    st.dataframe(pd.DataFrame(_cmp_rows), use_container_width=True, hide_index=True)
                    if _cmp_season is not None and _nfl_season is not None and _cmp_season != _nfl_season:
                        st.caption(f"Using {_cmp_season} regular-season stats until {_nfl_season} data is available.")
                    st.caption(
                        f"Season-to-date, through {_away_cmp['games_played']} ({_at}) / "
                        f"{_home_cmp['games_played']} ({_ht}) games played."
                    )
                else:
                    st.info("Team stats aren't available yet for one or both teams this season.")
                st.divider()

                # ── 3. Recent Form ─────────────────────────────────
                st.markdown("#### Recent Form")
                # Each team independently falls back to its own most
                # recent completed prior-season games when it has zero
                # completed current-season games -- a binary switch, not
                # a blended cross-season window (see
                # get_team_recent_form_with_fallback). It's expected for
                # one team to be on the current season and the other on
                # the prior season within the same matchup.
                _away_form, _away_form_season = get_team_recent_form_with_fallback(_nfl_season, _at, n=5)
                _home_form, _home_form_season = get_team_recent_form_with_fallback(_nfl_season, _ht, n=5)
                _rf_c1, _rf_c2 = st.columns(2)
                for _rf_col, _rf_team, _rf_form, _rf_form_season in (
                    (_rf_c1, _at, _away_form, _away_form_season),
                    (_rf_c2, _ht, _home_form, _home_form_season),
                ):
                    with _rf_col:
                        st.caption(_rf_team)
                        if _rf_form:
                            if _rf_form_season is not None and _nfl_season is not None and _rf_form_season != _nfl_season:
                                st.caption(f"Showing {_rf_form_season} recent form until {_nfl_season} games are completed.")
                            st.write(
                                f"Last {len(_rf_form['games'])}: {_rf_form['record']} · "
                                f"Avg {_rf_form['avg_scored']}-{_rf_form['avg_allowed']} "
                                f"(margin {_rf_form['avg_margin']:+.1f})"
                            )
                            # Same rows/order/count get_team_recent_form_
                            # with_fallback already returned above -- this
                            # only changes how they're displayed (a table
                            # instead of one st.caption per game).
                            _rf_table_rows = [
                                {
                                    "Week": _g["week"],
                                    "Opponent": f"{_g['location']} {_g['opponent']}",
                                    "Result": _g["result"],
                                    "Score": f"{_g['own_score']}-{_g['opp_score']}",
                                }
                                for _g in reversed(_rf_form["games"])
                            ]
                            st.dataframe(
                                pd.DataFrame(_rf_table_rows),
                                use_container_width=True, hide_index=True,
                                column_config={
                                    "Week": st.column_config.NumberColumn("Week", width="small"),
                                    "Result": st.column_config.TextColumn("Result", width="small"),
                                },
                            )
                        else:
                            st.caption("No completed games yet this season.")
                st.divider()

                # ── 4. Injuries ────────────────────────────────────
                # Official weekly NFL injury report (nflreadpy/nflverse)
                # — the same data source already relied on for Lineup
                # Analysis/Prop Research. Shows the most recent report
                # available for each team; report_status is shown exactly
                # as published (Out/Doubtful/Questionable/etc.).
                st.markdown("#### Injuries")
                _away_inj = get_team_injuries(_nfl_season, _at)
                _home_inj = get_team_injuries(_nfl_season, _ht)
                if not _away_inj and not _home_inj:
                    st.info("No players are currently listed on the official injury report for either team.")
                else:
                    _inj_c1, _inj_c2 = st.columns(2)
                    for _inj_col, _inj_team, _inj_rows in ((_inj_c1, _at, _away_inj), (_inj_c2, _ht, _home_inj)):
                        with _inj_col:
                            st.caption(_inj_team)
                            if _inj_rows:
                                st.dataframe(pd.DataFrame(_inj_rows), use_container_width=True, hide_index=True)
                            else:
                                st.caption("No players listed.")
                    st.caption("Source: official weekly NFL injury report (nflverse).")
                st.divider()

                # ── 5. Betting Splits ──────────────────────────────
                st.markdown("#### Betting Splits")
                st.info(
                    "Betting splits require a dedicated data provider and aren't "
                    "available through the current odds feed."
                )
