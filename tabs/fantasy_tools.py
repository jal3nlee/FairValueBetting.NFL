# tabs/fantasy_tools.py
# Consolidates the two previously-separate top-level fantasy tabs (Lineup
# Analysis, Fantasy Draft) into one "Fantasy Tools" tab with a two-section
# selector — Lineup Analysis is useful all season, Draft Rankings mainly
# around draft season, so both belong under one category instead of two
# permanent top-level slots. Reuses tabs/lineup_comparison.py::render and
# tabs/fantasy_draft.py::render exactly as-is — no logic here beyond
# navigation, same pattern as tabs/prop_leaderboard.py's own
# Player Research / Prop Leaderboard segmented_control.
import streamlit as st

from tabs import lineup_comparison, fantasy_draft


def render(supabase, now_utc):
    st.markdown("## Fantasy Tools")
    st.markdown(
        "<div style='opacity:0.7;font-size:0.95rem;margin:0 0 6px 0'>"
        "Weekly lineup research and fantasy draft rankings, in one place."
        "</div>",
        unsafe_allow_html=True,
    )

    _section = st.segmented_control(
        "Section", ["Lineup Analysis", "Draft Rankings"], default="Lineup Analysis",
        key="ft_section", label_visibility="collapsed",
    ) or "Lineup Analysis"

    st.divider()

    if _section == "Lineup Analysis":
        lineup_comparison.render(supabase, now_utc)
    else:
        fantasy_draft.render()
