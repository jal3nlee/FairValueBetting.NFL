# tabs/lineup_comparison.py
# User-facing name: "Lineup Analysis" — module filename kept as-is to
# avoid unnecessary import-risk across the app.
import streamlit as st
import pandas as pd

from core.nfl_player_search import render_nfl_player_search
from core.nfl_player_card import render_nfl_player_card
from core.lineup_data import build_player_comparison, get_team_game_context, PROP_LABELS
from core.nflverse_data import get_usage_samples, get_recent_games, LINEUP_USAGE_METRICS, METRIC_LABELS, PERCENT_METRICS
from core.nfl_player_context import render_opponent_defense_single, render_opponent_defense_multi

ROLES = ["Roster", "Bench", "Waiver"]


def _dash(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return v


def _tight_label(text: str):
    st.markdown(
        f"<div style='font-size:0.78rem;opacity:0.65;margin:0 0 1px 0;line-height:1'>{text}</div>",
        unsafe_allow_html=True,
    )


def _section_heading(text: str):
    st.markdown(
        f"<div style='font-size:1.15rem;font-weight:700;margin:4px 0 6px 0'>{text}</div>",
        unsafe_allow_html=True,
    )


def _selected_names(exclude_idx: int, n_slots: int) -> set[str]:
    names = set()
    for i in range(n_slots):
        if i == exclude_idx:
            continue
        p = st.session_state.get(f"lc_selected_{i}")
        if p:
            names.add(p["name"])
    return names


def render_player_slot(slot_idx: int, mode: str, n_slots: int, supabase, now_utc):
    _tight_label(f"Player {slot_idx + 1}")
    _taken = _selected_names(slot_idx, n_slots)
    _allowed = ["RB", "WR", "TE"] if mode == "FLEX" else ["QB", "RB", "WR", "TE"]

    p = render_nfl_player_search(f"lc_slot_{slot_idx}", allowed_positions=_allowed, taken_names=_taken)
    if not p:
        return None

    if mode == "Waiver":
        p["role"] = st.selectbox("Role", ROLES, key=f"lc_role_{slot_idx}")
    else:
        p["role"] = "Roster"

    ctx = get_team_game_context(supabase, p["team"], now_utc)
    p["context"] = ctx
    render_nfl_player_card(p, ctx, compact=False)

    if slot_idx > 0 and n_slots > 1:
        if st.button("Remove", key=f"lc_remove_{slot_idx}", use_container_width=True):
            st.session_state["lc_n_slots"] = n_slots - 1
            st.rerun()

    return p


def _fmt_val(v, is_pct):
    if v is None:
        return "—"
    return f"{v * 100:.0f}%" if is_pct else f"{v:.1f}"


def render_usage_role(enriched: list[dict], names: list[str], allow_mixed_positions: bool = False):
    """
    allow_mixed_positions=True (FLEX slot): shows the union of each
    compared player's own LINEUP_USAGE_METRICS, rather than requiring a
    single shared position. A metric that doesn't apply to a given
    player's own position shows "—" for that player (the existing
    missing-value convention) instead of being fabricated or suppressing
    the whole section.
    """
    _section_heading("Usage & Role")
    positions = {p["position"] for p in enriched}
    mixed = len(positions) > 1 and allow_mixed_positions
    if len(positions) == 1:
        metrics = LINEUP_USAGE_METRICS.get(next(iter(positions)), [])
    elif mixed:
        metrics = []
        _seen = set()
        for p in enriched:
            for m in LINEUP_USAGE_METRICS.get(p["position"], []):
                if m not in _seen:
                    _seen.add(m)
                    metrics.append(m)
    else:
        metrics = []
    if not metrics:
        if len(positions) > 1 and not allow_mixed_positions:
            st.caption("Select players at the same position to see matched usage metrics.")
        else:
            st.caption("Usage data temporarily unavailable.")
        return

    usage_by_player = {p["name"]: get_usage_samples(p["name"], p["team"], p["position"]) for p in enriched}
    if not any(usage_by_player.values()):
        st.caption("Usage data temporarily unavailable.")
        return

    if len(enriched) == 1:
        p = enriched[0]
        u = usage_by_player.get(p["name"], {})
        _rows = []
        for m in metrics:
            is_pct = m in PERCENT_METRICS
            entry = u.get(m, {})
            row = {"Metric": METRIC_LABELS.get(m, m)}
            for wkey, wlabel_base, wreq in [("season", "Season", None), ("last5", "Last 5", 5), ("last3", "Last 3", 3)]:
                w = entry.get(wkey, {})
                games = w.get("games", 0)
                col_label = wlabel_base if (wreq is None or games >= wreq) else f"{wlabel_base} ({games})"
                row[col_label] = _fmt_val(w.get("value"), is_pct)
            _rows.append(row)
        st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)
    else:
        _rows = []
        for m in metrics:
            is_pct = m in PERCENT_METRICS
            label = METRIC_LABELS.get(m, m)
            for wkey, wlabel in [("season", "Season"), ("last5", "Last 5"), ("last3", "Last 3")]:
                row = [f"{label} — {wlabel}"]
                for p in enriched:
                    if mixed and m not in LINEUP_USAGE_METRICS.get(p["position"], []):
                        row.append("—")  # metric doesn't apply to this player's own position
                        continue
                    w = usage_by_player.get(p["name"], {}).get(m, {}).get(wkey, {})
                    row.append(_fmt_val(w.get("value"), is_pct))
                _rows.append(row)
        st.dataframe(pd.DataFrame(_rows, columns=["Metric"] + names), use_container_width=True, hide_index=True)
    st.caption("Usage data: nflverse — current season only.")


def render_recent_games(enriched: list[dict]):
    _section_heading("Recent Games")
    _any = False
    for p in enriched:
        games = get_recent_games(p["name"], p["team"], p["position"], n=5)
        if not games:
            continue
        _any = True
        if len(enriched) > 1:
            st.markdown(f"**{p['name']}**")
        st.dataframe(pd.DataFrame(games), use_container_width=True, hide_index=True)
    if not _any:
        st.caption("No current-season game log available yet.")


def render(supabase, now_utc):
    st.markdown("## Lineup Analysis")
    st.markdown(
        "<div style='opacity:0.7;font-size:0.95rem;margin:0 0 6px 0'>"
        "Research weekly usage, player props, game environment, and matchup context for your fantasy lineup decisions."
        "</div>",
        unsafe_allow_html=True,
    )

    _tc1, _tc2, _spacer = st.columns([1.5, 1.5, 3], gap="small")
    with _tc1:
        _tight_label("Mode")
        _mode = st.segmented_control("Mode", ["Start/Sit", "FLEX", "Waiver"], default="Start/Sit",
                                      key="lc_mode", label_visibility="collapsed") or "Start/Sit"
    with _tc2:
        _tight_label("Scoring")
        _scoring = st.segmented_control("Scoring", ["PPR", "Half PPR", "Standard"], default="PPR",
                                         key="lc_scoring", label_visibility="collapsed") or "PPR"

    st.markdown("<div style='margin-top:4px'></div>", unsafe_allow_html=True)

    _n_slots = st.session_state.get("lc_n_slots", 1)
    _show_vs = _n_slots == 2

    if _show_vs:
        _slot_cols = st.columns([5, 0.5, 5], gap="small")
        _player_cols = [_slot_cols[0], _slot_cols[2]]
        with _slot_cols[1]:
            st.markdown(
                "<div style='display:flex;align-items:center;justify-content:center;height:100%;"
                "opacity:0.4;font-size:0.8rem;font-weight:600;margin-top:70px'>VS</div>",
                unsafe_allow_html=True,
            )
    elif _n_slots == 1:
        _player_cols = [st.container()]
    else:
        _player_cols = st.columns(_n_slots, gap="small")

    _confirmed_players = []
    for i, col in enumerate(_player_cols):
        with col:
            p = render_player_slot(i, _mode, _n_slots, supabase, now_utc)
            if p:
                _confirmed_players.append(p)

    st.markdown("<div style='margin-top:8px'></div>", unsafe_allow_html=True)
    if _confirmed_players:
        _add_col, _ = st.columns([1.1, 4.9])
        with _add_col:
            if st.button("+ Add Player", disabled=_n_slots >= 4, key="lc_add"):
                st.session_state["lc_n_slots"] = min(4, _n_slots + 1)
                st.rerun()

    if not _confirmed_players:
        st.caption("Select a player to begin your lineup analysis.")
        return

    with st.spinner("Loading market data..."):
        enriched = build_player_comparison(supabase, _confirmed_players, now_utc, scoring=_scoring)

    st.markdown("<div style='margin-top:6px'></div>", unsafe_allow_html=True)

    _is_flex = _mode == "FLEX"

    def _row(label, values):
        return [label] + [str(_dash(v)) for v in values]

    def _fmt_spread(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "—"
        return f"{v:+g}"

    def _fmt_pct(v):
        return "—" if v is None else f"{v * 100:.0f}%"

    def _fmt_fp(v):
        return "—" if v is None else f"{v:.1f}"

    _names = [p["name"] for p in enriched]

    _all_markets = sorted(set(m for p in enriched for m in p["props"].keys()))
    _has_any_prop_value = (
        any(p["props"].get(m) is not None for p in enriched for m in _all_markets)
        or any(p.get("td_fair_prob") is not None for p in enriched)
        or any(p.get("market_implied_fantasy_points") is not None for p in enriched)
    )
    _section_heading("Player Props")
    if not _all_markets and not _has_any_prop_value:
        st.caption("Player props are not available yet. Check back closer to kickoff.")
    else:
        # Market-Implied Fantasy Points and TD Fair Probability lead the
        # table (most decision-relevant), followed by the individual
        # sportsbook prop lines they're derived from.
        if len(enriched) == 1:
            p0 = enriched[0]
            _rows = [
                {"Prop": "Market-Implied Fantasy Points", "Line": _fmt_fp(p0.get("market_implied_fantasy_points"))},
                {"Prop": "Market-Implied TD Fair Probability", "Line": _fmt_pct(p0.get("td_fair_prob"))},
            ]
            _rows += [{"Prop": PROP_LABELS.get(m, m), "Line": p0["props"].get(m, "—")} for m in _all_markets]
            st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)
        else:
            _rows = [
                _row("Market-Implied Fantasy Points", [_fmt_fp(p.get("market_implied_fantasy_points")) for p in enriched]),
                _row("Market-Implied TD Fair Probability", [_fmt_pct(p.get("td_fair_prob")) for p in enriched]),
            ]
            _rows += [_row(PROP_LABELS.get(m, m), [p["props"].get(m) for p in enriched]) for m in _all_markets]
            st.dataframe(pd.DataFrame(_rows, columns=["Metric"] + _names), use_container_width=True, hide_index=True)
        st.caption(
            "Market-Implied Fantasy Points is a V1 estimate derived from current sportsbook prop lines and "
            "devigged touchdown probability, translated through standard fantasy scoring — not a statistical "
            "projection."
        )

    st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
    render_usage_role(enriched, _names, allow_mixed_positions=_is_flex)
    st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)
    render_recent_games(enriched)
    st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)

    _has_env = any(p["context"] for p in enriched)
    if _has_env:
        _section_heading("Game Environment")
        if len(enriched) == 1:
            ctx = enriched[0]["context"]
            _env = {
                "Opponent": ctx.get("opponent", "—"), "Home/Away": "Home" if ctx.get("is_home") else "Away",
                "Spread": _fmt_spread(ctx.get("spread")), "Game Total": ctx.get("game_total", "—"),
                "Team Implied Total": ctx.get("team_implied_total", "—"),
            }
            st.dataframe(pd.DataFrame([_env]), use_container_width=True, hide_index=True)
        else:
            _env_metrics = [
                ("Opponent", [p["context"].get("opponent") for p in enriched]),
                ("Spread", [_fmt_spread(p["context"].get("spread")) for p in enriched]),
                ("Game Total", [p["context"].get("game_total") for p in enriched]),
                ("Team Implied Total", [p["context"].get("team_implied_total") for p in enriched]),
            ]
            _rows = [_row(label, vals) for label, vals in _env_metrics]
            st.dataframe(pd.DataFrame(_rows, columns=["Metric"] + _names), use_container_width=True, hide_index=True)
        st.markdown("<div style='margin-top:14px'></div>", unsafe_allow_html=True)

    # ── Opponent Defense — now via the SHARED renderer, same code
    # Prop Research uses. Cannot take a player name, structurally. ──
    _section_heading("Opponent Defense")
    if len(enriched) == 1:
        st.markdown(
            "<div style='opacity:0.6;font-size:0.85rem;margin:-4px 0 8px 0'>"
            "Season defensive performance of the player's upcoming opponent.</div>",
            unsafe_allow_html=True,
        )
        render_opponent_defense_single(enriched[0]["context"].get("opponent"), enriched[0]["position"], _scoring)
    else:
        st.markdown(
            "<div style='opacity:0.6;font-size:0.85rem;margin:-4px 0 8px 0'>"
            "Season defensive performance of each player's upcoming opponent.</div>",
            unsafe_allow_html=True,
        )
        render_opponent_defense_multi(
            [{"name": p["name"], "position": p["position"], "opponent": p["context"].get("opponent")} for p in enriched],
            _scoring, allow_mixed_positions=_is_flex,
        )
