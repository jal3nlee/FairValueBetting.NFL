# tabs/fantasy_rankings.py
# Phase 1 Fantasy Rankings UI. NOT YET WIRED into tabs/fantasy_tools.py's
# "Section" selector or app.py — same "built, not registered" convention
# already used for tabs/player_research.py — pending the live-Supabase
# coverage validation called for before this goes in front of users (see
# the Fantasy Rankings implementation report). Wiring it in once that
# validation looks sound is a two-line change: add "Fantasy Rankings" to
# tabs/fantasy_tools.py's st.segmented_control options and an elif branch
# calling this module's render(supabase, now_utc).
import pandas as pd
import streamlit as st

from core.data_sources import get_date_window, infer_current_week_index
from core.nfl_prop_data_sources import get_upcoming_prop_event_ids
from core.nfl_fantasy_rankings import build_fantasy_rankings, SCORING_OPTIONS, RANKING_POSITIONS

_BASE_COLS = ["Rank", "Player", "Team", "Pos", "FVB Fantasy Pts"]


def _tight_label(text: str):
    st.markdown(
        f"<div style='font-size:0.78rem;opacity:0.65;margin:0 0 1px 0;line-height:1'>{text}</div>",
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=300, show_spinner=False)
def _load_rankings(_supabase, event_ids: tuple, window_start, window_end, scoring: str) -> dict:
    return build_fantasy_rankings(_supabase, list(event_ids), window_start, window_end, scoring)


def _detail_lines(detail_for_player: dict) -> list[str]:
    lines = []
    for label, d in detail_for_player.items():
        n = d.get("num_books")
        if "prob" in d:
            lines.append(
                f"**{label}:** {d['prob'] * 100:.1f}% fair Anytime TD probability "
                f"(λ = {d['lambda']:.3f} → {d['fantasy_pts']:.2f} pts) — {n} sportsbook{'s' if n != 1 else ''}"
            )
        else:
            lines.append(f"**{label}:** {d['value']:.1f} sportsbook-weighted consensus — {n} sportsbook{'s' if n != 1 else ''}")
    return lines


def render(supabase, now_utc):
    st.markdown("## Fantasy Rankings")
    st.markdown(
        "<div style='opacity:0.7;font-size:0.95rem;margin:0 0 10px 0'>"
        "FVB Fantasy Rankings translate sportsbook markets into fantasy points — "
        "not an independent player-performance projection. Every number below comes "
        "directly from a sportsbook-weighted consensus market."
        "</div>",
        unsafe_allow_html=True,
    )

    _r1c1, _r1c2 = st.columns([1.6, 1.6], gap="small")
    with _r1c1:
        _tight_label("Scoring")
        _scoring = st.segmented_control(
            "Scoring", SCORING_OPTIONS, default="PPR", key="fr_scoring", label_visibility="collapsed",
        ) or "PPR"
    with _r1c2:
        _tight_label("Position")
        _position = st.segmented_control(
            "Position", ["Overall"] + RANKING_POSITIONS, default="Overall",
            key="fr_position", label_visibility="collapsed",
        ) or "Overall"

    _wk = infer_current_week_index(now_utc)
    _week_label = "NFL Preseason" if _wk == 0 else f"NFL Week {_wk}"
    _window_choice = st.selectbox("Date Range", [_week_label, "Next 7 Days"], key="fr_window_choice")
    window_start, window_end, _sport_keys, caption_label = get_date_window(now_utc, _window_choice)
    # Same kickoff-cutoff reasoning as Fair Value Model's Player Props view
    # (tabs/fair_value_model.py::_render_player_props) — prop ingestion
    # stops polling an event once kickoff passes, so an already-started
    # event's last stored line is frozen pregame data, not fresh.
    _prop_window_start = max(window_start, now_utc)

    with st.spinner("Loading market-implied fantasy rankings..."):
        event_ids = get_upcoming_prop_event_ids(supabase, _prop_window_start.isoformat(), window_end.isoformat())
        result = _load_rankings(supabase, tuple(event_ids), _prop_window_start, window_end, _scoring)

    if _position == "Overall":
        frames = [result[p] for p in RANKING_POSITIONS if not result[p].empty]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not df.empty:
            df = df.sort_values("FVB Fantasy Pts", ascending=False).reset_index(drop=True)
            df["Rank"] = range(1, len(df) + 1)
    else:
        df = result.get(_position, pd.DataFrame())

    if df.empty:
        st.info(
            f"No players currently qualify for {_position} in {caption_label} under Phase 1's "
            "required-market coverage rule (a player must have sportsbook-weighted consensus "
            "for every required market at his position, from at least 2 sportsbooks each)."
        )
    else:
        stat_cols = [c for c in df.columns if c not in _BASE_COLS and not c.startswith("_")]
        display_cols = [c for c in _BASE_COLS if c in df.columns] + stat_cols
        st.dataframe(
            df[display_cols], use_container_width=True, hide_index=True,
            height=min(760, 46 + 35 * len(df)),
            column_config={
                "Rank": st.column_config.NumberColumn("RANK", width="small"),
                "Player": st.column_config.TextColumn("PLAYER", width="medium"),
                "Team": st.column_config.TextColumn("TEAM", width="small"),
                "Pos": st.column_config.TextColumn("POS", width="small"),
                "FVB Fantasy Pts": st.column_config.NumberColumn("FVB FANTASY PTS", width="small", format="%.2f"),
            },
        )

        st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)
        _player_options = df["Player"].tolist()
        _selected_player = st.selectbox(
            "View sportsbook-market detail for", _player_options, key="fr_detail_player",
        )
        _sel_row = df[df["Player"] == _selected_player].iloc[0]
        with st.expander(f"Market coverage — {_selected_player}", expanded=True):
            _pdetail = result.get("detail", {}).get(_sel_row.get("_player_key"), {})
            for line in _detail_lines(_pdetail):
                st.markdown(line)
            st.caption(
                "TD expectation is derived from the fair Anytime TD market using a Poisson "
                "approximation (λ = −ln(1 − P), fantasy points = λ × 6)."
            )

    _excluded = result.get("excluded", {})
    if any(_excluded.values()):
        _parts = [f"{v} {p}" for p, v in _excluded.items() if v]
        st.caption(
            f"Excluded from ranking (missing required sportsbook market coverage): {', '.join(_parts)}."
        )
    st.caption(
        "FVB Fantasy Rankings translate sportsbook markets into fantasy points. "
        "They are not an FVB fantasy projection model."
    )
