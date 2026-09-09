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
    show_label: bool = True,
) -> dict | None:
    """
    Renders the standardized Team | Position | Load Roster controls (one
    grouped row), followed by a full-width Player dropdown once the
    roster is loaded, and returns the selected player as {"name", "team",
    "position", "headshot_url"}, or None if no eligible player exists for
    the current Team/Position, or if the roster hasn't been loaded yet.
    Dropdown label format: "Player Name (Position)".

    Team/Position changes alone never trigger the roster fetch (a real
    network call — see core/lineup_data.py::get_players_by_team), any
    eligibility filtering of it, the Player dropdown, or any of the
    downstream Player Research rendering (Player Card, Prop Analysis,
    Player Context, matchup context) that runs off this function's
    return value — the user must press "Load Roster" first. Team/Position
    changes are "pending" (whatever the widgets currently show) until
    that press makes them "loaded" (this function's own cheap widget
    reads are the only work that happens on such a rerun; it returns
    None immediately after, before ever reaching get_players_by_team).

    Only ONE combination is ever "loaded" at a time, tracked as a single
    (team_abbr, position) tuple in session state — not a remembered set
    of every combination visited this session. Changing Team or Position
    away from that loaded combination — even back to one used earlier —
    always re-requires an explicit Load Roster press; it does not
    silently reactivate. get_players_by_team's own @st.cache_data caching
    still means pressing Load Roster again for a team fetched earlier
    this session costs no new network call, only the explicit click.
    A Player selector left over from a previous Team/Position never stays
    visible: changing either immediately invalidates the loaded state, so
    the selector (and everything downstream of it) disappears until Load
    Roster is pressed again for the new combination.
    """
    allowed_positions = allowed_positions or ["QB", "RB", "WR", "TE"]
    taken_names = taken_names or set()

    # show_label=False lets a caller that already renders its own "Player
    # Search" section heading (e.g. Prop Research) skip this internal
    # label instead of showing it twice. Defaults to True so Lineup
    # Analysis — which relies on this internal label as its only "Player
    # Search" text — is unaffected.
    if show_label:
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

    # Single loaded combo (pending team/position are just team_name/
    # team_abbr/position above, read fresh from the widgets every rerun) —
    # not a remembered set of every combination visited this session, so
    # changing away and back always re-requires pressing Load Roster.
    _loaded_key = f"{key_prefix}_loaded_combo"
    _current_selection = (team_abbr, position)
    _is_loaded = st.session_state.get(_loaded_key) == _current_selection

    with _c3:
        if not _is_loaded:
            if not st.button("Load Roster", key=f"{key_prefix}_load_btn", use_container_width=True):
                return None
            st.session_state[_loaded_key] = _current_selection

    # Roster fetch + Player dropdown live on their own full-width row below
    # the Team/Position/Load Roster group above, rather than sharing the
    # narrow third column — only ever reached once loaded (either just now
    # above, or already loaded on a prior pass for this exact Team/
    # Position). get_players_by_team is @st.cache_data-cached, so
    # pressing Load Roster again for an already-fetched team costs no new
    # network call.
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
