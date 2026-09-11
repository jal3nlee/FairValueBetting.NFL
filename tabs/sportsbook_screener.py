# tabs/sportsbook_screener.py
import pandas as pd
import streamlit as st

from core.odds_math import american_to_decimal, american_to_implied_prob, fmt_odds, parse_iso_dt_utc, EASTERN
from core.pipeline import MARKETS, build_books_df
from core.data_sources import fetch_market_lines, filter_by_window, get_date_window


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


def render(supabase, now_utc):
    st.markdown("## Sportsbook Screener")
    st.caption("Compare odds across every sportsbook for the market you want to bet.")

    from core.data_sources import infer_current_week_index
    _wk = infer_current_week_index(now_utc)
    _week_label = "NFL Preseason" if _wk == 0 else f"NFL Week {_wk}"
    _window_choice = st.selectbox(
        "Date Range", ["Today", _week_label, "Next 7 Days"], index=1, key="sc_window_choice",
    )
    window_start, window_end, sport_keys, caption_label = get_date_window(now_utc, _window_choice)

    _sc_mkt_col, _sc_fmt_col = st.columns(2)
    with _sc_mkt_col:
        _sc_mkt_sel = st.selectbox("Market", ["Moneyline", "Spread", "Total"], key="sc_mkt")
    with _sc_fmt_col:
        _odds_format = st.selectbox("Odds Format", ["American", "Decimal", "Implied %"], key="sc_odds_format")

    _sc_mkt_key = _sc_mkt_sel.lower()
    _sc_cfg = MARKETS[_sc_mkt_key]
    _sc_raw, _pulled = fetch_market_lines(supabase, sport_keys, _sc_cfg.db_market_key)
    _sc_raw = filter_by_window(_sc_raw, window_start, window_end)

    if _pulled:
        _sc_pull = max((parse_iso_dt_utc(p) for p in _pulled if p), default=None)
        if _sc_pull:
            st.caption(f"Last Updated: {_sc_pull.astimezone(EASTERN).strftime('%b %d  %I:%M %p ET')}")

    if _sc_raw.empty:
        st.info(f"No {_sc_mkt_sel} data available for {caption_label}.")
        return

    _sc_books_df = build_books_df(_sc_raw, _sc_cfg)
    if _sc_books_df.empty:
        st.info(f"No {_sc_mkt_sel} data could be processed.")
        return

    _seen_display: set = set()
    _sc_book_map: dict[str, str] = {}
    for _bk in sorted(_sc_books_df["book"].dropna().unique()):
        _dn = _sc_name(_bk)
        if _dn not in _seen_display:
            _sc_book_map[_bk] = _dn
            _seen_display.add(_dn)
    _sc_disp_to_key = {v: k for k, v in _sc_book_map.items()}
    _sc_all_display = sorted(_sc_book_map.values())
    # Always-visible pill/chip toggles (st.pills, selection_mode="multi")
    # instead of st.multiselect's dropdown-with-tags -- every sportsbook
    # is shown at once so a selected vs. deselected book is immediately
    # distinguishable, rather than only selected books appearing as tags
    # and everything else living inside a closed dropdown. width="stretch"
    # keeps this a full-width row, matching the rest of this filter area.
    _sc_selected_display = st.pills(
        "Sportsbooks", options=_sc_all_display, selection_mode="multi",
        default=_sc_all_display, key="sc_books", width="stretch",
    ) or []
    _sc_selected_keys = [_sc_disp_to_key[d] for d in _sc_selected_display if d in _sc_disp_to_key]

    if not _sc_selected_keys:
        st.info("Select at least one sportsbook to compare.")
        return

    _sc_filt = _sc_books_df[_sc_books_df["book"].isin(_sc_selected_keys)].copy()
    if _sc_filt.empty:
        st.info("No data for the selected sportsbooks.")
        return

    def _fmt_price(val):
        if pd.isna(val) or val is None:
            return "—"
        try:
            price = int(val)
            if _odds_format == "American":
                return fmt_odds(price)
            elif _odds_format == "Decimal":
                return f"{american_to_decimal(price):.2f}x"
            else:
                prob = american_to_implied_prob(price)
                return f"{prob * 100:.1f}%" if prob else "—"
        except Exception:
            return "—"

    _line_col = "total" if _sc_cfg.odds_api_market == "totals" else "line" if _sc_cfg.line_col else None

    _sc_games = (
        _sc_filt[["event_id", "home_team", "away_team", "commence_time"]]
        .drop_duplicates(subset=["event_id"])
        .sort_values("commence_time")
    )

    _total_picks = 0
    for _, _sc_game in _sc_games.iterrows():
        _eid = _sc_game["event_id"]
        _ht = _sc_game["home_team"]
        _at = _sc_game["away_team"]
        _gd = _sc_filt[_sc_filt["event_id"] == _eid]
        _away_logo = _logo_url(_at)
        _home_logo = _logo_url(_ht)

        _line_vals = (
            sorted(_gd[_line_col].dropna().unique()) if (_line_col and _line_col in _gd.columns) else [None]
        )

        with st.container(border=True):
            if _away_logo and _home_logo:
                st.markdown(
                    f"<img src='{_away_logo}' width='22' style='vertical-align:middle;margin-right:5px'/>"
                    f"**{_at}** @ "
                    f"<img src='{_home_logo}' width='22' style='vertical-align:middle;margin:0 5px'/>"
                    f"**{_ht}**",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(f"**{_at} @ {_ht}**")

            _condensed_rows = []
            _full_by_line = []
            for _lv in _line_vals:
                _ld = _gd[_gd[_line_col] == _lv] if _lv is not None else _gd
                if _sc_cfg.odds_api_market == "spreads":
                    _label_a = f"{_ht} {float(_lv):+g}" if _lv is not None else _ht
                    _label_b = f"{_at} {-float(_lv):+g}" if _lv is not None else _at
                elif _sc_cfg.odds_api_market == "totals":
                    _label_a = f"Over {float(_lv):.1f}" if _lv is not None else "Over"
                    _label_b = f"Under {float(_lv):.1f}" if _lv is not None else "Under"
                else:
                    _label_a = _ht
                    _label_b = _at

                for _side_label, _price_col in [(_label_a, _sc_cfg.price_a_col), (_label_b, _sc_cfg.price_b_col)]:
                    _best_price = None
                    _best_book = None
                    for _bk in _sc_selected_keys:
                        _bk_data = _ld[_ld["book"] == _bk]
                        if not _bk_data.empty:
                            _p = _bk_data[_price_col].iloc[0]
                            if pd.notna(_p):
                                _pi = int(_p)
                                if _best_price is None or _pi > _best_price:
                                    _best_price = _pi
                                    _best_book = _sc_name(_bk)
                    _condensed_rows.append({
                        "Pick": _side_label,
                        "Best Book": _best_book or "—",
                        "Best Odds": _fmt_price(_best_price),
                    })
                    _total_picks += 1
                _full_by_line.append((_lv, _label_a, _label_b, _ld))

            st.dataframe(
                pd.DataFrame(_condensed_rows), use_container_width=True, hide_index=True,
                column_config={
                    "Pick": st.column_config.TextColumn("Pick", width="medium"),
                    "Best Book": st.column_config.TextColumn(
                        "Best Book", help="Sportsbook with the highest available price for this pick.",
                    ),
                    "Best Odds": st.column_config.TextColumn(
                        "Best Odds", help="Best price found among the selected sportsbooks.",
                    ),
                },
            )

            with st.expander("Compare all sportsbooks", expanded=False):
                for _lv, _label_a, _label_b, _ld in _full_by_line:
                    if len(_full_by_line) > 1:
                        st.caption(f"Line: {_lv}")
                    _compare_rows = []
                    for _side_label, _price_col in [(_label_a, _sc_cfg.price_a_col), (_label_b, _sc_cfg.price_b_col)]:
                        _row = {"Pick": _side_label}
                        for _bk in _sc_selected_keys:
                            _dn = _sc_name(_bk)
                            _bk_data = _ld[_ld["book"] == _bk]
                            _p = _bk_data[_price_col].iloc[0] if not _bk_data.empty else None
                            _row[_dn] = _fmt_price(_p)
                        _compare_rows.append(_row)
                    st.dataframe(
                        pd.DataFrame(_compare_rows), use_container_width=True, hide_index=True,
                        column_config={
                            "Pick": st.column_config.TextColumn("Pick", width="medium"),
                            **{
                                _sc_name(_bk): st.column_config.TextColumn(
                                    _sc_name(_bk), help=f"Odds at {_sc_name(_bk)}."
                                )
                                for _bk in _sc_selected_keys
                            },
                        },
                    )

    st.caption(
        f"{_total_picks} betting options  |  "
        f"{len(_sc_selected_keys)} sportsbook{'s' if len(_sc_selected_keys) != 1 else ''} — {caption_label}."
    )
