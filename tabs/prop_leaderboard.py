# tabs/prop_leaderboard.py
# User-facing name: "Prop Research" — module filename kept as-is to
# avoid unnecessary import-risk across the app.
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
    LINEUP_USAGE_METRICS, METRIC_LABELS, PERCENT_METRICS,
)
from core.lineup_data import (
    get_team_game_context, fetch_player_props_for_event, get_consensus_prop_line, NFL_TEAMS,
)

_ABBR_TO_FULLNAME = {v.upper(): k for k, v in NFL_TEAMS.items()}

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


def _opponent_for(team_abbr: str, supabase, now_utc):
    try:
        full_name = _ABBR_TO_FULLNAME.get(team_abbr)
        if not full_name:
            return "—"
        ctx = get_team_game_context(supabase, full_name, now_utc)
        return ctx.get("opponent", "—") if ctx else "—"
    except Exception:
        return "—"


def _fmt_usage_val(v, is_pct):
    if v is None:
        return "—"
    return f"{v * 100:.0f}%" if is_pct else f"{v:.1f}"


def render_leaderboard_view(supabase, now_utc):
    _c1, _c2, _c3, _c4 = st.columns([1.8, 1.2, 1.2, 1.6])
    with _c1:
        stat_label = st.selectbox("Prop", list(PROP_STAT_MAP.keys()), key="pl_stat")
    with _c2:
        side = st.selectbox("Over/Under", ["Over", "Under"], key="pl_side")
    with _c3:
        line = st.number_input("Prop Line", min_value=0.0, value=49.5, step=0.5, key="pl_line")
    with _c4:
        sample_label = st.selectbox("Sample Size", list(SAMPLE_OPTIONS.keys()), index=1, key="pl_sample")

    _run = st.button("Find Top 10", type="primary", key="pl_run")
    st.markdown(f"### {side} {line:g} {stat_label}")
    st.caption(sample_label)

    if not _run:
        st.info("Set your filters above and click **Find Top 10** to run the search.")
        return

    with st.spinner("Scanning current-season player data..."):
        results = build_prop_leaderboard(stat_label, side, line, sample_label)

    if not results:
        st.info(
            "No players qualified with a full sample for this stat, line, and sample size. "
            "Try a shorter sample or a different threshold."
        )
        return

    avg_label = PROP_AVG_LABEL.get(stat_label, "Avg")
    rows = []
    for i, r in enumerate(results, 1):
        opp = _opponent_for(r["team"], supabase, now_utc)
        rows.append({
            "Rank": i, "Player": r["player"], "Team": r["team"], "Pos": r["position"], "Opp": opp,
            "Hit Rate": r["hit_rate"] / 100.0, "Record": f"{r['hits']} / {r['games']}", avg_label: r["avg"],
        })

    df = pd.DataFrame(rows)
    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={"Hit Rate": st.column_config.ProgressColumn("Hit Rate", format="%.0f%%", min_value=0.0, max_value=1.0)},
    )
    if any(r["pushes"] > 0 for r in results):
        st.caption("Pushes (exact line matches) are excluded from both hits and the sample denominator.")


def _render_prop_analysis(player: dict, ctx: dict, supabase, now_utc):
    _available = [s for s, positions in PROP_POSITION_MAP.items() if player["position"] in positions]
    if player["position"] in ("WR", "TE", "RB"):
        _available = _available + list(PLAYER_SEARCH_EXTRA_STATS.keys())
    if not _available:
        st.info("No supported prop stats for this position.")
        return

    _r1c1, _r1c2, _r1c3 = st.columns([1.6, 1.0, 1.4], gap="small")
    with _r1c1:
        st.caption("Prop")
        _picked_label = st.selectbox("Prop", _available, key="ps_stat_pick", label_visibility="collapsed")
    stat_field = PROP_STAT_MAP.get(_picked_label) or PLAYER_SEARCH_EXTRA_STATS.get(_picked_label)

    odds_market_key = PROP_LABEL_TO_ODDS_MARKET.get(_picked_label)
    prop_rows = []
    consensus_line = None
    if odds_market_key and ctx.get("event_id"):
        prop_rows = fetch_player_props_for_event(ctx["event_id"], player["position"])
        consensus_line = get_consensus_prop_line(prop_rows, player["name"], odds_market_key)

    with _r1c2:
        st.caption("Prop Line")
        _threshold = st.number_input(
            "Prop Line", min_value=0.0,
            value=float(consensus_line) if consensus_line is not None else 0.5,
            step=0.5, key="ps_threshold", label_visibility="collapsed",
        )
    with _r1c3:
        st.caption("Sample Size")
        _sample_label = st.selectbox(
            "Sample Size", ["Last 5 Games", "Last 10 Games", "Season"], index=1,
            key="ps_sample", label_visibility="collapsed",
        )

    _side_col, _ = st.columns([1.0, 3.0])
    with _side_col:
        _side = st.segmented_control("Side", ["Over", "Under"], default="Over",
                                      key="ps_side", label_visibility="collapsed") or "Over"

    st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)

    st.markdown("<div style='font-size:1.05rem;font-weight:700;margin:0 0 2px 0'>Current Market</div>", unsafe_allow_html=True)
    if not odds_market_key:
        st.caption(f"{_picked_label} isn't tracked by sportsbooks — research the line above manually.")
    elif consensus_line is None:
        st.caption("Player props are not available yet. Check back closer to kickoff.")
    else:
        st.markdown(f"**{_picked_label} — {consensus_line:g}** (consensus)")
        _book_rows = {}
        for r in prop_rows:
            if r["player"].strip().lower() != player["name"].strip().lower() or r["market"] != odds_market_key:
                continue
            b = r["book"]
            _book_rows.setdefault(b, {"Sportsbook": _sc_name(b), "Line": r.get("line"), "Over": None, "Under": None})
            if r.get("side") in ("Over", "Yes"):
                _book_rows[b]["Over"] = _fmt_odds(r.get("price"))
            elif r.get("side") in ("Under", "No"):
                _book_rows[b]["Under"] = _fmt_odds(r.get("price"))
        if _book_rows:
            st.dataframe(pd.DataFrame(list(_book_rows.values())), use_container_width=True, hide_index=True)
        else:
            st.caption("No individual sportsbook prices available for this market yet.")

    st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)

    st.markdown("## Prop Hit Rate")
    st.caption("See how often this player has cleared the selected prop line.")

    _sample_n = {"Last 5 Games": 5, "Last 10 Games": 10, "Season": None}[_sample_label]
    _full_log = get_player_game_log(player["name"], player["team"], stat_field, n_games=None)
    _dashboard_log = _full_log[:_sample_n] if _sample_n else _full_log

    render_prop_hit_rate_dashboard(
        _picked_label, _side, _threshold, _dashboard_log, _sample_label,
        current_season=get_current_season(),
    )

    if _full_log:
        st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
        st.markdown("### Recent Prop Results")
        st.caption("Line = your selected research line, not a historical sportsbook line.")
        _cur_season = get_current_season()
        _log_rows = []
        for g in _full_log[:10]:
            _wk = f"W{g['week']}" if g.get("season") == _cur_season else f"W{g['week']} {g.get('season')}"
            _log_rows.append({
                "Week": _wk, "Opponent": g["opponent"], _picked_label: g["value"], "Line": _threshold,
                "Result": ("Over" if g["value"] > _threshold else "Push" if g["value"] == _threshold else "Under"),
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
    player = render_nfl_player_search("ps_slot", allowed_positions=["QB", "RB", "WR", "TE"])
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
