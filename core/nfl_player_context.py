# core/nfl_player_context.py
# Shared NFL context rendering used by BOTH Lineup Analysis and Prop
# Research's Player Context — one implementation, not two copies that
# can drift apart. Covers: season-aware week labeling, and the
# opponent-defense table (which never needs the player's name, only
# their position and upcoming opponent).
import streamlit as st
import pandas as pd

from core.nfl_defense_data import get_opponent_defense, POSITION_DEFENSE_METRICS


def format_nfl_week(week, season, current_season, compact: bool = False) -> str:
    """
    Current-season game: 'W8'. Prior-season game: 'W17 2025' (or
    'W17 '25' if compact=True, for chart labels). Uses the game's own
    recorded season — never inferred from the week number.
    """
    if season is not None and current_season is not None and season != current_season:
        return f"W{week} '{str(season)[-2:]}" if compact else f"W{week} {season}"
    return f"W{week}"


def render_opponent_defense_single(opponent: str | None, position: str, scoring: str = "PPR"):
    """
    One player's opponent-defense table — general season-long defensive
    stats for the opponent, aggregated across ALL offensive players they've
    faced league-wide. Not the selected player's personal history against
    that team. The column header is JUST the team name — no "vs Position"
    framing, since that phrasing kept reading as player-vs-team.
    """
    if not opponent:
        st.caption("No opponent this week (bye week).")
        return

    _def = get_opponent_defense(opponent, position, scoring)
    _metric_set = POSITION_DEFENSE_METRICS.get(position, [])
    if not _def or not _metric_set:
        st.caption("Opponent defensive data is not available yet.")
        return

    _rows = [{"Metric": label, opponent: _def.get(field, "—")} for field, label in _metric_set]
    st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)
    st.caption("Defensive data: nflverse")


def render_opponent_defense_multi(players_with_opponents: list[dict], scoring: str = "PPR", allow_mixed_positions: bool = False):
    """
    players_with_opponents: [{"name", "position", "opponent"}, ...] — used
    for Lineup Analysis's multi-player comparison. Same general opponent
    defensive stats as above, matched to each selected player's position.
    Column headers are just each opponent's team name.

    allow_mixed_positions=True (FLEX slot): each player is evaluated
    against their OWN position's defensive metrics, never forced onto a
    shared positional metric set. The displayed rows are the union of
    each compared player's own POSITION_DEFENSE_METRICS entries (e.g. a
    RB vs WR comparison shows "RB Fantasy Pts Allowed / G" and "WR
    Fantasy Pts Allowed / G" as separate rows, each populated only for
    the player whose position it belongs to — never merged into one
    shared row, never fabricated for the other player).
    """
    if all(not p.get("opponent") for p in players_with_opponents):
        st.caption("No opponent this week (bye week).")
        return

    positions = {p["position"] for p in players_with_opponents}
    if len(positions) != 1 and not allow_mixed_positions:
        st.caption("Select players at the same position to see matched defensive context.")
        return

    # Union of each player's own position-specific metric rows, in the
    # order each position first contributes them.
    metric_set = []
    _seen_fields = set()
    for p in players_with_opponents:
        for field, label in POSITION_DEFENSE_METRICS.get(p["position"], []):
            if field not in _seen_fields:
                _seen_fields.add(field)
                metric_set.append((field, label))
    if not metric_set:
        st.caption("Opponent defensive data is not available yet.")
        return

    def_by_player = {
        p["name"]: get_opponent_defense(p.get("opponent"), p["position"], scoring) for p in players_with_opponents
    }
    headers = [p.get("opponent") or "Bye Week" for p in players_with_opponents]

    rows = []
    for field, label in metric_set:
        row = [label]
        for p in players_with_opponents:
            own_fields = {f for f, _ in POSITION_DEFENSE_METRICS.get(p["position"], [])}
            if field not in own_fields:
                row.append("—")  # metric belongs to a different position -- not fabricated for this player
                continue
            d = def_by_player.get(p["name"])
            v = d.get(field) if d else None
            row.append(v if v is not None else "—")
        rows.append(row)

    st.dataframe(pd.DataFrame(rows, columns=["Metric"] + headers), use_container_width=True, hide_index=True)
    st.caption("Defensive data: nflverse")
