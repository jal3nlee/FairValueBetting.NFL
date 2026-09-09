# core/prop_hit_rate_dashboard.py
# Shared "Prop Hit Rate" dashboard used by MLB Player Search and NFL
# Prop Leaderboard's Player Search. Labels are dynamic: "Over Rate" /
# "Under Rate" based on the selected side; the chart legend is always
# Over / Under / Push (a fixed classification, not side-dependent).
import streamlit as st
import pandas as pd


def _classify(value: float, line: float, side: str) -> str:
    """Fixed classification — Over/Under/Push — independent of which
    side the user selected. The user's selected side is used only to
    pick which of these counts as a 'success' for rate/streak purposes."""
    if value == line:
        return "Push"
    return "Over" if value > line else "Under"


def _current_streak(game_log_newest_first: list[dict], line: float) -> tuple[int, str] | tuple[None, None]:
    """
    Walks forward from the most recent game, counting consecutive games
    on the SAME actual side (Over or Under) — this is the observed run,
    independent of which side the user has selected to research.
    Push breaks the streak.
    """
    streak_class = None
    count = 0
    for g in game_log_newest_first:
        cls = _classify(g["value"], line, None)
        if cls == "Push":
            break
        if streak_class is None:
            streak_class, count = cls, 1
        elif cls == streak_class:
            count += 1
        else:
            break
    if streak_class is None:
        return None, None
    return count, streak_class


def _week_label(g: dict, week_label_key: str, current_season) -> str:
    week = g.get(week_label_key)
    season = g.get("season")
    if season is not None and current_season is not None and season != current_season:
        return f"W{week} {season}"
    return f"W{week}"


def render_prop_hit_rate_dashboard(
    stat_label: str,
    side: str,
    line: float,
    game_log_newest_first: list[dict],
    sample_label: str,
    week_label_key: str = "week",
    opponent_key: str = "opponent",
    value_key: str = "value",
    current_season=None,
):
    """
    game_log_newest_first: list of dicts with at least week_label_key,
    opponent_key, value_key — MOST RECENT GAME FIRST. An optional
    "season" key on each entry enables cross-season week labeling
    (W17 2025 vs W3) — harmless/no-op if absent or all one season.
    """
    if not game_log_newest_first:
        st.caption("No current-season game data is available for this prop yet.")
        return

    hits = misses = pushes = 0
    for g in game_log_newest_first:
        cls = _classify(g[value_key], line, side)
        if cls == "Push":
            pushes += 1
        elif cls == side:
            hits += 1
        else:
            misses += 1
    decided = hits + misses
    if decided == 0:
        st.caption("Every game in this sample was an exact push — no Over/Under result to summarize.")
        return

    rate = hits / decided * 100
    avg_val = sum(g[value_key] for g in game_log_newest_first) / len(game_log_newest_first)
    streak_count, streak_class = _current_streak(game_log_newest_first, line)
    side_label = "Over" if side == "Over" else "Under"

    with st.container(border=True):
        st.markdown(
            f"<div style='font-size:0.85rem;opacity:0.65;letter-spacing:0.04em;"
            f"text-transform:uppercase;margin-bottom:2px'>"
            f"{side.upper()} {line:g} {stat_label.upper()}</div>",
            unsafe_allow_html=True,
        )
        _sc1, _sc2 = st.columns([1.3, 2])
        with _sc1:
            st.markdown(
                f"<div style='font-size:2.6rem;font-weight:800;line-height:1'>{rate:.0f}%</div>"
                f"<div style='opacity:0.6;font-size:0.85rem;margin-top:2px'>{side_label} Rate</div>",
                unsafe_allow_html=True,
            )
        with _sc2:
            st.markdown(
                f"<div style='margin-top:0.4rem;font-size:0.95rem'>"
                f"{side_label} in {hits} of {decided if decided == len(game_log_newest_first) else f'the last {decided}'} games</div>",
                unsafe_allow_html=True,
            )
            _m1, _m2 = st.columns(2)
            _m1.metric(f"Avg. {stat_label}", f"{avg_val:.1f}")
            if streak_count is not None:
                _word = "over" + ("s" if streak_count != 1 else "") if streak_class == "Over" else "under" + ("s" if streak_count != 1 else "")
                _m2.metric("Current Streak", f"{streak_count} {_word}")
            else:
                _m2.metric("Current Streak", "—")

        st.markdown("<div style='margin-top:0.5rem'></div>", unsafe_allow_html=True)
        try:
            import altair as alt
            _chart_rows = list(reversed(game_log_newest_first))
            _chart_df = pd.DataFrame(_chart_rows)
            _chart_df["Label"] = _chart_df.apply(lambda r: _week_label(r, week_label_key, current_season), axis=1)
            _chart_df["Result"] = _chart_df[value_key].apply(lambda v: _classify(v, line, side))
            _bars = (
                alt.Chart(_chart_df).mark_bar(size=18)
                .encode(
                    x=alt.X("Label:N", sort=None, title=None, axis=alt.Axis(labelAngle=-45)),
                    y=alt.Y(f"{value_key}:Q", title=stat_label),
                    color=alt.Color(
                        "Result:N",
                        scale=alt.Scale(domain=["Over", "Under", "Push"], range=["#2ecc71", "#7a7a7a", "#f0ad4e"]),
                        legend=alt.Legend(title=None, orient="top"),
                    ),
                    tooltip=["Label", f"{opponent_key}", f"{value_key}", "Result"],
                )
            )
            _rule = (
                alt.Chart(pd.DataFrame({"y": [line]}))
                .mark_rule(color="#e74c3c", strokeDash=[4, 3]).encode(y="y:Q")
            )
            # Text mark anchored to the most recent (rightmost) bar's own
            # x-category, rather than a fixed pixel position -- works
            # regardless of how many games are in the sample, with no
            # extra scale/axis changes and no effect on bar colors,
            # values, or week ordering.
            _last_label = _chart_df["Label"].iloc[-1]
            _rule_label = (
                alt.Chart(pd.DataFrame({"y": [line], "x": [_last_label], "text": [f"Prop Line {line:g}"]}))
                .mark_text(align="right", baseline="bottom", dy=-4, color="#e74c3c", fontSize=11)
                .encode(x=alt.X("x:N", sort=None), y="y:Q", text="text:N")
            )
            st.altair_chart((_bars + _rule + _rule_label).properties(height=220), use_container_width=True)
        except Exception:
            _chart_rows = list(reversed(game_log_newest_first))
            _indicators = " ".join(
                {"Over": "🟢O", "Under": "⚪U", "Push": "🟠P"}[_classify(g[value_key], line, side)]
                for g in _chart_rows
            )
            st.caption(_indicators)
