# core/nfl_player_search.py
# One standardized NFL player-selection pattern, used everywhere a single
# player needs picking: Lineup Analysis and Prop Leaderboard's Player
# Search subview. Team -> Position -> Player, no free-text search.
import streamlit as st

from core.lineup_data import get_players_by_team, get_players_by_position, NFL_TEAMS

_POSITION_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4, "DST": 5}
DEFAULT_NFL_TEAM = sorted(NFL_TEAMS.keys())[0]


def _sort_key(p: dict):
    return (_POSITION_ORDER.get(p.get("position", ""), 99), p.get("name", ""))


def render_nfl_player_search(
    key_prefix: str,
    allowed_positions: list[str] | None = None,
    taken_names: set[str] | None = None,
) -> dict | None:
    """
    Renders the standardized Team | Position | Load Players | Player
    controls and returns the selected player as {"name", "team",
    "position", "headshot_url"}, or None if no eligible player exists for
    the current Team/Position, or if the roster hasn't been loaded yet.
    Dropdown label format: "Player Name (Position)".

    Team/Position changes alone never trigger the roster fetch (a real
    network call — see core/lineup_data.py::get_players_by_team) — the
    user must press "Load Players" first. This also means a Player
    selector left over from a previous Team/Position never stays visible:
    changing either immediately invalidates the "loaded" state below, so
    the selector disappears until Load Players is pressed again for the
    new combination. Re-selecting ANY Team/Position combination that was
    already loaded earlier in this session (not just the most recent one)
    skips straight back to the Player selector, no redundant click — the
    set of loaded combinations is remembered for the session, and
    get_players_by_team's own caching means revisiting one costs nothing
    extra anyway.
    """
    allowed_positions = allowed_positions or ["QB", "RB", "WR", "TE"]
    taken_names = taken_names or set()

    st.markdown(
        "<div style='font-size:0.95rem;font-weight:600;margin:0 0 4px 0'>Player Search</div>",
        unsafe_allow_html=True,
    )

    _c1, _c2, _c3 = st.columns([2.2, 1.2, 2.6])
    with _c1:
        team_name = st.selectbox(
            "Team", sorted(NFL_TEAMS.keys()),
            index=sorted(NFL_TEAMS.keys()).index(DEFAULT_NFL_TEAM),
            key=f"{key_prefix}_team", label_visibility="collapsed",
        )
    team_abbr = NFL_TEAMS.get(team_name)

    with _c2:
        position = st.selectbox(
            "Position", ["All"] + allowed_positions,
            key=f"{key_prefix}_position", label_visibility="collapsed",
        )

    _loaded_key = f"{key_prefix}_loaded_combos"
    _current_selection = (team_abbr, position)
    if _loaded_key not in st.session_state:
        st.session_state[_loaded_key] = set()

    with _c3:
        if _current_selection not in st.session_state[_loaded_key]:
            if not st.button("Load Players", key=f"{key_prefix}_load_btn", use_container_width=True):
                return None
            st.session_state[_loaded_key].add(_current_selection)

        # Roster fetch only ever runs once loaded — either just now (button
        # pressed this pass) or already loaded on a prior pass for this
        # exact Team/Position. get_players_by_team is @st.cache_data-cached,
        # so revisiting an already-loaded Team/Position costs nothing extra.
        roster = get_players_by_team(team_abbr) if team_abbr else []
        if position != "All":
            roster = [p for p in roster if p.get("position") == position]
        else:
            roster = [p for p in roster if p.get("position") in allowed_positions]
        roster = [p for p in roster if p.get("name") and p["name"] not in taken_names]
        roster = sorted(roster, key=_sort_key)

        if not roster:
            st.selectbox("Player", ["No eligible players"], key=f"{key_prefix}_player_empty",
                         label_visibility="collapsed", disabled=True)
            return None
        _labels = {f"{p['name']} ({p['position']})": p for p in roster}
        _picked_label = st.selectbox(
            "Player", list(_labels.keys()), key=f"{key_prefix}_player", label_visibility="collapsed",
        )
        p = _labels[_picked_label]

    return {"name": p["name"], "team": team_name, "position": p.get("position", ""), "headshot_url": p.get("headshot_url")}
