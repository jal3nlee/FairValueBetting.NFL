# tabs/prop_leaderboard.py
# User-facing name: "Prop Research" — module filename kept as-is to
# avoid unnecessary import-risk across the app.
import html

import streamlit as st
import pandas as pd

from core.nfl_player_search import render_nfl_player_search
from core.nfl_player_card import render_nfl_player_card
from core.prop_hit_rate_dashboard import render_prop_hit_rate_dashboard
from core.nfl_player_context import render_opponent_defense_single
from core.nflverse_data import (
    PROP_STAT_MAP, PROP_POSITION_MAP, PROP_AVG_LABEL, SAMPLE_OPTIONS,
    PLAYER_SEARCH_EXTRA_STATS, PROP_LABEL_TO_ODDS_MARKET,
    build_prop_leaderboard, get_player_game_log, get_current_season,
    get_usage_samples, get_expanded_season_stats, get_recent_games,
    get_current_week_team_names, format_prop_line_value,
    LINEUP_USAGE_METRICS, METRIC_LABELS, PERCENT_METRICS,
)
from core.lineup_data import (
    get_team_game_context, fetch_player_props_for_event, get_consensus_prop_line,
)
from core.nfl_prop_market_config import normalize_player_key

# Prop Leaderboard player-card styling -- ported from FVB-Platform's
# core/branding.py brand colors (core/stat_leaderboard.py's card CSS
# reuses these same two hex values), matching "FVB styling conventions"
# rather than this app's own theme primary (#4A79BD). This app has no
# core/branding.py module, so the two constants are defined locally
# instead of imported, same as the local NFL_TEAM_ABBR/_logo_url copy
# below (an already-established duplication pattern in this codebase --
# see tabs/matchup_center.py, tabs/parlay_builder.py,
# tabs/sportsbook_screener.py, tabs/market_movers.py).
_LB_NAVY = "#2B337C"
_LB_BLUE = "#4978BC"
_LB_GAME_LOG_ROW_HEIGHT = 35  # approximate st.dataframe row height, px
_LB_GAME_LOG_HEADER_HEIGHT = 38
_LB_GAME_LOG_MAX_HEIGHT = 280  # bounded/scrollable cap for long Season samples

# Position-aware default Prop selection -- WR/TE default to Receiving
# Yards even though PROP_POSITION_MAP's dict order would otherwise land
# on "Rushing Yards" first for WR (both markets list WR as eligible, and
# Rushing Yards appears first in that dict).
_POSITION_DEFAULT_PROP = {
    "QB": "Passing Yards", "RB": "Rushing Yards", "WR": "Receiving Yards", "TE": "Receiving Yards",
}

# Priority 3 fallback: used only when neither a current sportsbook line
# nor a current-season game log average is available. These are static,
# non-market, non-statistical starting points for research -- not a Fair
# Value claim.
_PROP_STATIC_FALLBACK = {
    "Passing Yards": 225.5, "Passing TDs": 1.5, "Interceptions": 0.5,
    "Pass Attempts": 32.5, "Completions": 20.5,
    "Rushing Yards": 45.5, "Rushing Attempts": 10.5,
    "Receiving Yards": 45.5, "Receptions": 3.5, "Targets": 5.5,
    "Anytime TD": 0.5,
}

SPORTSBOOK_DISPLAY = {
    "fanduel": "FanDuel", "draftkings": "DraftKings", "betmgm": "BetMGM",
    "caesars": "Caesars", "espnbet": "ESPN Bet", "fanatics": "Fanatics",
    "hardrockbet": "Hard Rock Bet", "betrivers": "BetRivers", "bovada": "Bovada",
}


def _sc_name(book: str) -> str:
    return SPORTSBOOK_DISPLAY.get(str(book).lower(), str(book).replace("_", " ").title())


def _fmt_odds(price):
    if price is None:
        return "—"
    return f"+{int(price)}" if price > 0 else str(int(price))


def _fmt_usage_val(v, is_pct):
    if v is None:
        return "—"
    return f"{v * 100:.0f}%" if is_pct else f"{v:.1f}"


# Same team-abbreviation map and ESPN CDN logo URL pattern already
# duplicated per-tab elsewhere in this app (matchup_center.py,
# parlay_builder.py, sportsbook_screener.py, market_movers.py) -- used
# here only as a card-headshot fallback when a player has no headshot_url.
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


def _inject_leaderboard_card_css():
    """Ported from FVB-Platform's core/stat_leaderboard.py -- same class
    names, same layout rules, same brand colors, including the desktop/
    mobile compact-metrics-row breakpoint and the headshot circle's
    zoomed-in image treatment."""
    st.markdown(
        f"""
        <style>
        .fvb-lb-card {{
            border: 1px solid rgba(35, 31, 32, 0.12);
            border-radius: 10px;
            padding: 16px 20px;
            margin-bottom: 4px;
            background: #ffffff;
        }}
        .fvb-lb-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 24px;
            flex-wrap: wrap;
        }}
        .fvb-lb-left {{
            display: flex;
            align-items: center;
            gap: 12px;
            min-width: 180px;
            flex: 1 1 200px;
        }}
        .fvb-lb-rank {{
            font-weight: 700;
            color: {_LB_NAVY};
            font-size: 14px;
            width: 26px;
            flex-shrink: 0;
        }}
        .fvb-lb-headshot-wrap {{
            width: 68px;
            height: 68px;
            border-radius: 50%;
            overflow: hidden;
            background: #eef0f5;
            flex-shrink: 0;
        }}
        .fvb-lb-headshot {{
            width: 100%;
            height: 100%;
            object-fit: cover;
            /* The circle/container size above is unchanged — this only
            scales the image content itself so a face fills more of the
            circle, cropped by the wrapper's own overflow:hidden. */
            transform: scale(1.15);
        }}
        .fvb-lb-name {{
            font-weight: 600;
            font-size: 15px;
            color: #231F20;
            line-height: 1.3;
        }}
        .fvb-lb-sub {{
            font-size: 12px;
            color: #6b7280;
            line-height: 1.3;
        }}
        .fvb-lb-center {{
            flex: 1 1 220px;
            min-width: 180px;
        }}
        .fvb-lb-label {{
            font-size: 11px;
            color: #6b7280;
            text-transform: uppercase;
            letter-spacing: .03em;
            margin-bottom: 4px;
        }}
        .fvb-lb-track {{
            background: #eef0f5;
            border-radius: 6px;
            height: 8px;
            width: 100%;
            overflow: hidden;
        }}
        .fvb-lb-fill {{
            background: {_LB_BLUE};
            height: 100%;
            border-radius: 6px;
        }}
        .fvb-lb-hr-value {{
            font-size: 13px;
            font-weight: 600;
            color: #231F20;
            margin-top: 4px;
        }}
        .fvb-lb-record {{
            font-size: 12px;
            color: #6b7280;
        }}
        .fvb-lb-right {{
            text-align: right;
            min-width: 90px;
            flex: 0 1 110px;
        }}
        .fvb-lb-avg-value {{
            font-size: 18px;
            font-weight: 700;
            color: {_LB_NAVY};
        }}
        .fvb-lb-metrics-desktop {{
            display: flex;
            align-items: center;
            gap: 24px;
            flex: 1 1 auto;
            flex-wrap: wrap;
        }}
        .fvb-lb-metrics-mobile {{
            display: none;
        }}
        .fvb-lb-mobile-metrics-row {{
            display: flex;
            gap: 10px;
        }}
        .fvb-lb-mobile-metric {{
            flex: 1 1 0;
            min-width: 0;
        }}
        @media (max-width: 480px) {{
            .fvb-lb-card {{
                padding: 12px 14px;
            }}
            .fvb-lb-headshot-wrap {{
                width: 58px;
                height: 58px;
            }}
            .fvb-lb-row {{
                flex-direction: column;
                align-items: stretch;
                gap: 10px;
            }}
            .fvb-lb-left {{
                flex: 0 0 auto;
            }}
            .fvb-lb-metrics-desktop {{
                display: none;
            }}
            .fvb-lb-metrics-mobile {{
                display: block;
                width: 100%;
            }}
            .fvb-lb-mobile-metrics-row {{
                margin-bottom: 8px;
            }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _leaderboard_card_html(rank: int, r: dict, avg_label: str, headshot_or_logo: str | None) -> str:
    pct = max(0.0, min(100.0, r["hit_rate"]))
    name = html.escape(str(r["player"]))
    team = html.escape(str(r["_team_display"]))
    position = r.get("position")
    sub = f"{team} • {html.escape(str(position))}" if position else team
    img_html = (
        f'<div class="fvb-lb-headshot-wrap"><img class="fvb-lb-headshot" '
        f'src="{html.escape(headshot_or_logo)}" onerror="this.parentElement.style.display=\'none\'"></div>'
        if headshot_or_logo else ""
    )
    avg_label_esc = html.escape(avg_label)
    return f"""
    <div class="fvb-lb-card">
      <div class="fvb-lb-row">
        <div class="fvb-lb-left">
          <div class="fvb-lb-rank">#{rank}</div>
          {img_html}
          <div>
            <div class="fvb-lb-name">{name}</div>
            <div class="fvb-lb-sub">{sub}</div>
          </div>
        </div>
        <div class="fvb-lb-metrics-desktop">
          <div class="fvb-lb-center">
            <div class="fvb-lb-label">Hit Rate</div>
            <div class="fvb-lb-track"><div class="fvb-lb-fill" style="width:{pct:.0f}%"></div></div>
            <div class="fvb-lb-hr-value">{pct:.0f}%</div>
            <div class="fvb-lb-record">{r['hits']} / {r['games']}</div>
          </div>
          <div class="fvb-lb-right">
            <div class="fvb-lb-label">{avg_label_esc}</div>
            <div class="fvb-lb-avg-value">{r['avg']}</div>
          </div>
        </div>
        <div class="fvb-lb-metrics-mobile">
          <div class="fvb-lb-mobile-metrics-row">
            <div class="fvb-lb-mobile-metric">
              <div class="fvb-lb-label">Hit Rate</div>
              <div class="fvb-lb-hr-value">{pct:.0f}%</div>
            </div>
            <div class="fvb-lb-mobile-metric">
              <div class="fvb-lb-label">Record</div>
              <div class="fvb-lb-hr-value">{r['hits']} / {r['games']}</div>
            </div>
            <div class="fvb-lb-mobile-metric">
              <div class="fvb-lb-label">{avg_label_esc}</div>
              <div class="fvb-lb-avg-value">{r['avg']}</div>
            </div>
          </div>
          <div class="fvb-lb-track"><div class="fvb-lb-fill" style="width:{pct:.0f}%"></div></div>
        </div>
      </div>
    </div>
    """


def _leaderboard_game_log_dataframe(game_log: list) -> pd.DataFrame:
    """The exact sample games already computed by build_prop_leaderboard,
    reshaped (not recalculated) into the four display columns -- no
    filtering, resorting, or refetching happens here."""
    return pd.DataFrame(
        [
            {
                "Opponent": g["label"],
                "Week / Date": g["date_or_week"],
                "Result": g["result"],
                "Outcome": g["outcome"],
            }
            for g in game_log
        ]
    )


def render_leaderboard_view(supabase, now_utc):
    """
    All filters (Prop, Over/Under, Prop Line, Sample Size, Team) live
    inside one st.form: none of them determine another control's options
    or defaults (unlike Player Research's Prop selector, which does), so
    there's no reason for any of them to be reactive. Editing any filter
    causes zero script rerun until "Find Top 10" is pressed. The
    expensive league-wide scan (build_prop_leaderboard) runs only from a
    submission snapshot (st.session_state["pl_submitted"]), never from
    live widget values, so a filter edit after results exist never
    silently recomputes them — previous results stay visible, captioned
    with exactly what they're showing, until the next submit.
    """
    # Only teams with a game anywhere in the current NFL week -- the same
    # set candidate eligibility below is checked against (get_current_
    # week_team_names / get_current_week_teams), so the Team filter can
    # never offer a bye team that could only ever return zero results.
    _team_pairs = get_current_week_team_names(now_utc)
    _team_options = ["All Teams"] + [name for name, _abbr in _team_pairs]

    with st.form(key="pl_form"):
        _c1, _c2, _c3 = st.columns(3)
        with _c1:
            _stat_label_input = st.selectbox("Prop", list(PROP_STAT_MAP.keys()), key="pl_stat")
        with _c2:
            _side_input = st.selectbox("Over/Under", ["Over", "Under"], key="pl_side")
        with _c3:
            _line_input = st.number_input("Prop Line", min_value=0.0, value=49.5, step=0.5, key="pl_line")

        # Eligible positions for the selected prop are still enforced
        # internally by build_prop_leaderboard via PROP_POSITION_MAP
        # (unchanged) — there's just no separate Position narrowing
        # control in this UI.
        _f1, _f2, _f3 = st.columns(3)
        with _f1:
            _sample_label_input = st.selectbox("Sample Size", list(SAMPLE_OPTIONS.keys()), index=1, key="pl_sample")
        with _f2:
            _team_choice_input = st.selectbox("Team", _team_options, key="pl_team")
        with _f3:
            st.markdown("<div style='height:1.9rem'></div>", unsafe_allow_html=True)
            _run = st.form_submit_button("Find Top 10", type="primary", use_container_width=True)

    if _run:
        st.session_state["pl_submitted"] = {
            "stat_label": _stat_label_input, "side": _side_input, "line": _line_input,
            "sample_label": _sample_label_input, "team_choice": _team_choice_input,
        }

    _sub = st.session_state.get("pl_submitted")
    if not _sub:
        st.info("Set your filters above and click **Find Top 10** to run the search.")
        return

    _line_display = format_prop_line_value(_sub["stat_label"], _sub["line"])
    st.markdown(f"### {_sub['stat_label']} | {_sub['side']} {_line_display}")
    st.caption(_sub["sample_label"])

    _inject_leaderboard_card_css()

    with st.spinner("Scanning current player data..."):
        # limit=None: the full sorted candidate list, so Team/Position can
        # narrow it BEFORE truncating to a top 10 for display — filtering
        # an already-truncated top 10 could return sparse/misleading
        # results for a team or position that didn't happen to place in
        # the unfiltered league-wide top 10. Candidate construction,
        # eligibility, hit-rate calculation, and ranking are all unchanged
        # inside build_prop_leaderboard; only truncation moved here.
        all_results = build_prop_leaderboard(
            _sub["stat_label"], _sub["side"], _sub["line"], _sub["sample_label"], now_utc, limit=None,
        )

    if not all_results:
        st.info(
            "No players qualified with a full sample for this stat, line, and sample size. "
            "Try a shorter sample or a different threshold."
        )
        return

    # Team filtering: fully local/in-memory over the already-ranked full
    # candidate list — no new roster/player/API calls, no rescan. "team"
    # is already on every result row from build_prop_leaderboard, so this
    # is pure filtering, not a lookup.
    results = all_results
    _team_abbr_by_name = dict(_team_pairs)
    if _sub["team_choice"] != "All Teams":
        _team_abbr = _team_abbr_by_name.get(_sub["team_choice"])
        results = [r for r in results if r["team"] == _team_abbr]
    results = results[:10]

    if not results:
        st.info(f"No qualifying {_sub['stat_label'].lower()} results for {_sub['team_choice']} with the current filters.")
        return

    # Full team names for display only — reuses the same already-fetched
    # (full_name, abbr) pairs the Team filter dropdown is built from, no
    # new lookup. Internal filtering above still compares abbreviations,
    # matching the "team" field build_prop_leaderboard actually returns.
    _team_name_by_abbr = {abbr: name for name, abbr in _team_pairs}

    # Ranked player cards (ported from FVB-Platform's core/
    # stat_leaderboard.py) instead of a plain st.dataframe -- every number
    # shown (hit rate, record, average, and each Recent Performance row)
    # comes straight from build_prop_leaderboard's already-computed result
    # dict; nothing here recalculates a sample or a hit rate independently.
    avg_label = PROP_AVG_LABEL.get(_sub["stat_label"], "Avg")
    for i, r in enumerate(results, 1):
        r["_team_display"] = _team_name_by_abbr.get(r["team"], r["team"])
        headshot_or_logo = r.get("headshot_url") or _logo_url(r["_team_display"])
        with st.container():
            st.markdown(_leaderboard_card_html(i, r, avg_label, headshot_or_logo), unsafe_allow_html=True)
            game_log = r.get("game_log") or []
            with st.expander("Recent Performance", expanded=False):
                if game_log:
                    _gl_df = _leaderboard_game_log_dataframe(game_log)
                    _gl_height = min(
                        len(_gl_df) * _LB_GAME_LOG_ROW_HEIGHT + _LB_GAME_LOG_HEADER_HEIGHT,
                        _LB_GAME_LOG_MAX_HEIGHT,
                    )
                    st.dataframe(_gl_df, use_container_width=True, hide_index=True, height=_gl_height)
                else:
                    st.caption("No individual game detail available for this result.")

    if any(r["pushes"] > 0 for r in results):
        st.caption("Pushes (exact line matches) are excluded from both hits and the sample denominator.")


def _render_prop_analysis(player: dict, ctx: dict, supabase, now_utc):
    """
    Prop stays reactive (outside st.form) because it determines both the
    position-specific option set and which current-market line/quick-picks
    apply -- a form defers ALL its widgets' effects until submit, so a
    Prop selector inside one couldn't visibly refresh the market-line
    preview on selection. Everything below it that has no cross-control
    dependency (Prop Line, Sample Size, Side) lives inside a form; only
    pressing "Run Analysis" triggers the Prop Hit Rate / Recent Prop
    Results section, using the exact values submitted at that moment --
    not whatever the widgets currently show if they've since changed.
    """
    _available = [s for s, positions in PROP_POSITION_MAP.items() if player["position"] in positions]
    if player["position"] in ("WR", "TE", "RB"):
        _available = _available + list(PLAYER_SEARCH_EXTRA_STATS.keys())
    if not _available:
        st.info("No supported prop stats for this position.")
        return

    _default_prop = _POSITION_DEFAULT_PROP.get(player["position"])
    _default_prop_index = _available.index(_default_prop) if _default_prop in _available else 0

    st.caption("Prop")
    _picked_label = st.selectbox(
        "Prop", _available, index=_default_prop_index, key="ps_stat_pick", label_visibility="collapsed",
    )
    stat_field = PROP_STAT_MAP.get(_picked_label) or PLAYER_SEARCH_EXTRA_STATS.get(_picked_label)

    # Current sportsbook market data for this player/prop -- a single
    # live per-event Odds API call (cached 30 min), already the same path
    # Lineup Analysis uses via fetch_player_props_for_event/
    # get_consensus_prop_line. Not routed through the Phase 1 Supabase
    # player_prop_snapshots/player_prop_lines pipeline or its
    # devig/consensus/EV/Kelly engine -- this is a lightweight research
    # default, not a Fair Value calculation, and doesn't need that
    # heavier machinery just to populate a starting threshold.
    odds_market_key = PROP_LABEL_TO_ODDS_MARKET.get(_picked_label)
    prop_rows = []
    market_line = None
    _book_lines = []
    if odds_market_key and ctx.get("event_id"):
        prop_rows = fetch_player_props_for_event(ctx["event_id"], player["position"])
        market_line = get_consensus_prop_line(prop_rows, player["name"], odds_market_key)
        _target_key = normalize_player_key(player["name"])
        _book_lines = sorted({
            float(r["line"]) for r in prop_rows
            if normalize_player_key(r.get("player")) == _target_key
            and r.get("market") == odds_market_key and r.get("line") is not None
        })

    # Priority 2 fallback: current-season per-game average for this exact
    # stat. Computed here (for the live default preview, tied to whatever
    # Prop is currently selected) and re-derived below from the submitted
    # Prop specifically, reusing this same result when they match rather
    # than fetching twice.
    _full_log = get_player_game_log(player["name"], player["team"], stat_field, n_games=None)
    _season_avg = None
    if _full_log:
        _vals = [g["value"] for g in _full_log if g.get("value") is not None]
        if _vals:
            _season_avg = round(sum(_vals) / len(_vals) * 2) / 2

    if market_line is not None:
        _default_line, _line_source = float(market_line), "market"
    elif _season_avg is not None:
        _default_line, _line_source = _season_avg, "season"
    else:
        _default_line, _line_source = _PROP_STATIC_FALLBACK.get(_picked_label, 0.5), "fallback"

    # Reinitialize the research threshold only when Player or Prop
    # actually changes (a fresh widget key). _threshold_key must be
    # computed here regardless of which Current Market branch fires below
    # -- the quick-pick block further down writes into it before the Prop
    # Line widget with this same key is created inside the form.
    _threshold_key = f"ps_threshold__{player['name']}__{_picked_label}"

    # ── Current Market: one prominent line + a short "N sportsbooks
    # posted" secondary line, instead of a separate "Current Market"
    # heading stacked on top of a second "Current Market Line: ..."
    # line repeating the same information. ──
    if not odds_market_key:
        st.markdown("<div style='font-size:1.05rem;font-weight:700;margin:8px 0 2px 0'>Current Market</div>", unsafe_allow_html=True)
        st.caption(f"{_picked_label} isn't tracked by sportsbooks — research the line below manually.")
        _book_rows = {}
    elif market_line is None:
        st.markdown("<div style='font-size:1.05rem;font-weight:700;margin:8px 0 2px 0'>Current Market</div>", unsafe_allow_html=True)
        if _line_source == "season":
            st.caption(f"No current sportsbook market yet — line defaulted to this season's average ({_season_avg:g}).")
        else:
            st.caption("Player props are not available yet. Check back closer to kickoff.")
        _book_rows = {}
    else:
        _book_rows = {}
        for r in prop_rows:
            if normalize_player_key(r.get("player")) != normalize_player_key(player["name"]) or r["market"] != odds_market_key:
                continue
            b = r["book"]
            _book_rows.setdefault(b, {"Sportsbook": _sc_name(b), "Line": r.get("line"), "Over": None, "Under": None})
            if r.get("side") in ("Over", "Yes"):
                _book_rows[b]["Over"] = _fmt_odds(r.get("price"))
            elif r.get("side") in ("Under", "No"):
                _book_rows[b]["Under"] = _fmt_odds(r.get("price"))
        _n_books = len(_book_rows)
        st.markdown(
            f"<div style='font-size:1.3rem;font-weight:800;margin:8px 0 0 0'>Current Market: {market_line:g}</div>"
            f"<div style='opacity:0.6;font-size:0.85rem;margin-top:2px'>"
            f"{_n_books} sportsbook{'s' if _n_books != 1 else ''} currently posted</div>",
            unsafe_allow_html=True,
        )
        if _book_rows:
            with st.expander("View sportsbook lines"):
                st.dataframe(pd.DataFrame(list(_book_rows.values())), use_container_width=True, hide_index=True)
        else:
            st.caption("No individual sportsbook prices available for this market yet.")

    # Market Lines quick-picks -- shown after the Current Market line, on
    # the exact same trigger condition as before (more than one distinct
    # posted line); only the visual position moved. Still writes into
    # _threshold_key before the Prop Line widget below is created, so a
    # pick is reflected immediately.
    if len(_book_lines) > 1:
        _quick_pick_key = f"ps_quickline__{player['name']}__{_picked_label}"
        _quick_applied_key = f"{_quick_pick_key}__applied"
        st.caption("Market Lines")
        _quick_pick = st.segmented_control(
            "Market Lines", [f"{l:g}" for l in _book_lines],
            key=_quick_pick_key, label_visibility="collapsed",
        )
        if _quick_pick is not None and st.session_state.get(_quick_applied_key) != _quick_pick:
            st.session_state[_threshold_key] = float(_quick_pick)
            st.session_state[_quick_applied_key] = _quick_pick

    st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)

    # ── Everything below has no cross-control dependency, so it's the
    # only part that benefits from being deferred to one explicit submit
    # instead of a rerun per keystroke/click. Editing Prop Line, Sample
    # Size, or Side inside this form causes no script rerun at all until
    # "Run Analysis" is pressed.
    with st.form(key=f"ps_form__{player['name']}"):
        _fc1, _fc2 = st.columns([1.0, 1.0], gap="small")
        with _fc1:
            st.caption("Prop Line")
            _threshold_input = st.number_input(
                "Prop Line", min_value=0.0, value=_default_line,
                step=0.5, key=_threshold_key, label_visibility="collapsed",
            )
        with _fc2:
            st.caption("Sample Size")
            _sample_label_input = st.selectbox(
                "Sample Size", ["Last 5 Games", "Last 10 Games", "Season"], index=1,
                key="ps_sample", label_visibility="collapsed",
            )
        _side_col, _btn_col = st.columns([1.0, 1.0], gap="small")
        with _side_col:
            st.caption("Side")
            _side_input = st.segmented_control("Side", ["Over", "Under"], default="Over",
                                                key="ps_side", label_visibility="collapsed") or "Over"
        with _btn_col:
            st.markdown("<div style='height:1.9rem'></div>", unsafe_allow_html=True)
            _submitted = st.form_submit_button("Run Analysis", type="primary", use_container_width=True)

    _submission_key = f"ps_submitted__{player['name']}"
    if _submitted:
        st.session_state[_submission_key] = {
            "picked_label": _picked_label, "stat_field": stat_field,
            "side": _side_input, "threshold": _threshold_input, "sample_label": _sample_label_input,
        }

    _sub = st.session_state.get(_submission_key)
    if not _sub:
        st.info("Set your Prop, Prop Line, and Sample Size above, then click **Run Analysis** to see Prop Hit Rate results.")
        return

    st.markdown("<div style='margin-top:6px'></div>", unsafe_allow_html=True)
    st.caption(
        f"Analysis: {_sub['picked_label']} · {_sub['side']} {_sub['threshold']:g} · {_sub['sample_label']}"
    )

    st.markdown("## Prop Hit Rate")
    st.caption("See how often this player has cleared the selected prop line.")

    _sample_n = {"Last 5 Games": 5, "Last 10 Games": 10, "Season": None}[_sub["sample_label"]]
    if _sample_n is None:
        # "Season" stays current-season only, matching its own label --
        # reuse the game log already fetched above when the submitted
        # Prop is still the currently-selected one (the common case);
        # only re-fetch (still a cheap, cached call) if the user changed
        # Prop after their last submission without resubmitting yet.
        _dashboard_log = _full_log if _sub["stat_field"] == stat_field else get_player_game_log(
            player["name"], player["team"], _sub["stat_field"], n_games=None,
        )
    else:
        # "Last 5/10 Games": reaches into the prior season for enough of
        # its most recent games to fill the requested sample when the
        # current season alone doesn't have that many yet (e.g. only 2
        # games played so far this season on a "Last 10 Games" request).
        # Always fetched fresh rather than reusing _full_log, which is
        # always current-season-only and feeds the Current-Season
        # Average default above -- that must stay current-season only,
        # distinct from this historical hit-rate sample. Both
        # _weekly_rows_for_player and _season_rows_for_player_by_name are
        # st.cache_data-cached, so a repeat call here on an unrelated
        # rerun is a cache hit, not a new nflverse fetch.
        _dashboard_log = get_player_game_log(
            player["name"], player["team"], _sub["stat_field"],
            n_games=_sample_n, cross_season=True,
        )

    render_prop_hit_rate_dashboard(
        _sub["picked_label"], _sub["side"], _sub["threshold"], _dashboard_log, _sub["sample_label"],
        current_season=get_current_season(),
    )

    if _dashboard_log:
        st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
        st.markdown("### Recent Prop Results")
        # Iterates _dashboard_log (not a separately-unbounded log) so
        # this table always shows exactly the games the Prop Hit Rate
        # dashboard above just used -- for "Last 5/10 Games" that's the
        # full (possibly cross-season) sample; for "Season" it's capped
        # at 10 most recent, same as before this fix.
        _cur_season = get_current_season()
        _log_rows = []
        for g in _dashboard_log[:10]:
            _wk = f"W{g['week']}" if g.get("season") == _cur_season else f"W{g['week']} {g.get('season')}"
            _log_rows.append({
                # "Line" intentionally not repeated per row here — the
                # submitted research line is already shown in the caption
                # above and in the Prop Hit Rate header, so a column
                # showing the same identical value on every row would
                # just be redundant clutter.
                "Week": _wk, "Opponent": g["opponent"], _sub["picked_label"]: g["value"],
                "Result": ("Over" if g["value"] > _sub["threshold"] else "Push" if g["value"] == _sub["threshold"] else "Under"),
            })
        st.dataframe(pd.DataFrame(_log_rows), use_container_width=True, hide_index=True)


def _render_player_context(player: dict, ctx: dict):
    st.markdown("#### Season Stats")
    expanded = get_expanded_season_stats(player["name"], player["team"], player["position"])
    if not expanded:
        st.caption("No current-season stats available yet.")
    else:
        st.dataframe(pd.DataFrame([expanded]), use_container_width=True, hide_index=True)

    st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)

    st.markdown("#### Recent Game Stats")
    metrics = LINEUP_USAGE_METRICS.get(player["position"], [])
    if metrics:
        u = get_usage_samples(player["name"], player["team"], player["position"])
        if u:
            _rows = []
            for m in metrics:
                is_pct = m in PERCENT_METRICS
                entry = u.get(m, {})
                row = {"Metric": METRIC_LABELS.get(m, m)}
                for wkey, wlabel_base, wreq in [("season", "Season", None), ("last5", "Last 5", 5), ("last3", "Last 3", 3)]:
                    w = entry.get(wkey, {})
                    games = w.get("games", 0)
                    col_label = wlabel_base if (wreq is None or games >= wreq) else f"{wlabel_base} ({games})"
                    row[col_label] = _fmt_usage_val(w.get("value"), is_pct)
                _rows.append(row)
            st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)

    st.markdown("##### Recent Game Log")
    games = get_recent_games(player["name"], player["team"], player["position"], n=10)
    if games:
        st.dataframe(pd.DataFrame(games), use_container_width=True, hide_index=True)
    else:
        st.caption("No current-season game log available yet.")

    st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)

    st.markdown("#### Opponent / Matchup Stats")
    opponent = ctx.get("opponent")
    if not opponent:
        st.caption("No opponent this week (bye week).")
        return

    _side_word = "vs" if ctx.get("is_home") else "@"
    st.caption(f"{_side_word} {opponent}")
    _env = {
        "Spread": ctx.get("spread", "—"), "Game Total": ctx.get("game_total", "—"),
        "Team Implied Total": ctx.get("team_implied_total", "—"),
    }
    st.dataframe(pd.DataFrame([_env]), use_container_width=True, hide_index=True)

    st.markdown("<div style='margin-top:8px'></div>", unsafe_allow_html=True)
    # ── Shared renderer — same code Lineup Analysis uses. ──
    render_opponent_defense_single(opponent, player["position"], "PPR")


def render_player_research_view(supabase, now_utc):
    st.markdown("### Player Search")
    # render_nfl_player_search returns None both while the user is still
    # choosing Team/Position (before pressing Load Players) and when a
    # loaded roster has no eligible players — the widget itself already
    # gives the right feedback for each case (a Load Players button, or a
    # disabled "No eligible players" selectbox), so nothing else to show here.
    player = render_nfl_player_search("ps_slot", allowed_positions=["QB", "RB", "WR", "TE"], show_label=False)
    if not player:
        return

    ctx = get_team_game_context(supabase, player["team"], now_utc)
    render_nfl_player_card(player, ctx, compact=False)

    st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)

    with st.expander("Prop Analysis", expanded=True):
        _render_prop_analysis(player, ctx, supabase, now_utc)

    st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)

    # Player Context is genuinely deferred (not just visually collapsed):
    # in the Streamlit version this app runs, code inside a plain
    # st.expander executes on every rerun regardless of its open/closed
    # state, so a plain expander here would still call
    # get_expanded_season_stats/get_usage_samples/get_recent_games/
    # get_opponent_defense on every unrelated interaction on the page.
    # This explicit open/closed toggle, keyed per player, means that work
    # only runs when the user actually opens it — and each newly selected
    # player starts closed, since the key changes with the player.
    _pc_key = f"ps_pc_open__{player['team']}__{player['name']}"
    _pc_open = st.session_state.get(_pc_key, False)
    if st.button(f"Player Context {'▾' if _pc_open else '▸'}", key=f"{_pc_key}_btn"):
        _pc_open = not _pc_open
        st.session_state[_pc_key] = _pc_open

    if _pc_open:
        with st.container(border=True):
            _render_player_context(player, ctx)


def render(supabase, now_utc):
    st.markdown("## Prop Research")
    st.markdown(
        "<div style='opacity:0.7;font-size:0.95rem;margin:0 0 6px 0'>"
        "Research NFL player props by individual player or historical hit rates."
        "</div>",
        unsafe_allow_html=True,
    )

    _view = st.segmented_control(
        "View", ["Player Research", "Prop Leaderboard"], default="Player Research",
        key="pl_view", label_visibility="collapsed",
    ) or "Player Research"

    if _view == "Player Research":
        render_player_research_view(supabase, now_utc)
    else:
        render_leaderboard_view(supabase, now_utc)
