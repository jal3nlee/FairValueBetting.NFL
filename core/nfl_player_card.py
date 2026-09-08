# core/nfl_player_card.py
import streamlit as st
import pandas as pd

from core.odds_math import parse_iso_dt_utc, EASTERN
from core.lineup_data import NFL_TEAMS
from core.nflverse_data import get_card_season_stats, get_current_season

_ABBR = {full: abbr.upper() for full, abbr in NFL_TEAMS.items()}


def _team_abbr(team_full_name: str) -> str:
    return _ABBR.get(team_full_name, team_full_name)


def _fmt_game_time(commence_iso: str | None) -> str | None:
    if not commence_iso:
        return None
    dt = parse_iso_dt_utc(commence_iso)
    if not dt:
        return None
    et = dt.astimezone(EASTERN)
    return et.strftime("%a %I:%M %p ET").replace(" 0", " ")


def render_nfl_player_card(
    player: dict,
    context: dict | None = None,
    compact: bool = False,
    spread: float | None = None,
    game_total: float | None = None,
    team_total: float | None = None,
):
    """
    compact=True: Prop Leaderboard's Player Search — tight identity card.
    compact=False: Lineup Analysis — MLB-style two-region card, season
    stats rendered as a real single-row table via st.dataframe (matches
    the MLB card's ERA | WHIP | K | Wins layout).
    """
    context = context or {}
    team_abbr = _team_abbr(player.get("team", ""))
    headshot = player.get("headshot_url")

    opponent = context.get("opponent")
    opp_line = None
    if opponent:
        side = "vs" if context.get("is_home") else "@"
        opp_abbr = _team_abbr(opponent)
        game_time = _fmt_game_time(context.get("commence_time"))
        opp_line = f"{side} {opp_abbr}" + (f" · {game_time}" if game_time else "")
    elif "opponent" in context:
        opp_line = "Bye Week"

    if compact:
        headshot_size = 72
        img_html = (
            f"<img src='{headshot}' style='width:{headshot_size}px;height:{headshot_size}px;"
            f"object-fit:contain;flex-shrink:0;'/>"
            if headshot else
            f"<div style='width:{headshot_size}px;height:{headshot_size}px;border-radius:8px;"
            f"background:rgba(128,128,128,0.15);flex-shrink:0;'></div>"
        )
        lines = [
            f"<div style='font-weight:700;font-size:1.05rem;line-height:1.25'>{player['name']}</div>",
            f"<div style='opacity:0.65;font-size:0.85rem;line-height:1.3'>{player.get('position','')} · {team_abbr}</div>",
        ]
        if opp_line:
            lines.append(f"<div style='opacity:0.6;font-size:0.8rem;line-height:1.3'>{opp_line}</div>")

        st.markdown(
            f"<div style='display:flex;align-items:center;gap:16px;max-width:520px;"
            f"border:1px solid rgba(128,128,128,0.25);border-radius:12px;padding:18px;margin:2px 0;'>"
            f"{img_html}"
            f"<div style='min-width:0;'>{''.join(lines)}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        return

    season_stats = get_card_season_stats(player["name"], player.get("team", ""), player.get("position", ""))
    season_year = get_current_season() or "—"
    headshot_size = 88

    with st.container(border=True):
        _left_col, _right_col = st.columns([2.2, 1.8], gap="medium")

        with _left_col:
            _photo_col, _info_col = st.columns([0.8, 2.6])
            with _photo_col:
                if headshot:
                    st.markdown(
                        f"<div style='width:{headshot_size}px;height:{headshot_size}px;display:flex;"
                        f"align-items:center;justify-content:center;overflow:hidden;'>"
                        f"<img src='{headshot}' style='width:100%;height:100%;object-fit:contain;'/></div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='width:{headshot_size}px;height:{headshot_size}px;border-radius:8px;"
                        f"background:rgba(128,128,128,0.15);'></div>",
                        unsafe_allow_html=True,
                    )
            with _info_col:
                st.markdown(
                    f"<div style='font-size:1.4rem;font-weight:700;line-height:1.15;margin-top:2px'>{player['name']}</div>"
                    f"<div style='opacity:0.7;font-size:0.9rem;line-height:1.3;margin-top:2px'>"
                    f"{player.get('position','')} · {team_abbr}</div>",
                    unsafe_allow_html=True,
                )
                # Always reserve this line (falls back to the same "—"
                # missing-value convention used elsewhere) rather than
                # omitting it — otherwise a player with no context data
                # renders a visibly shorter card next to one that has it.
                st.markdown(
                    f"<div style='opacity:0.6;font-size:0.85rem;margin-top:2px'>{opp_line or '—'}</div>",
                    unsafe_allow_html=True,
                )

        with _right_col:
            # Same reasoning as opp_line above: always render the "Season
            # Stats" heading and reserve its row, so two compared cards
            # stay the same height/structure even when one player has no
            # stats yet -- the missing side gets the existing caption
            # convention instead of an empty column.
            st.markdown(
                f"<div style='opacity:0.6;font-size:0.78rem;font-weight:600;letter-spacing:0.03em;"
                f"text-transform:uppercase;margin-bottom:4px'>{season_year} Season Stats</div>",
                unsafe_allow_html=True,
            )
            if season_stats:
                _df = pd.DataFrame([{s["label"]: s["value"] for s in season_stats}])
                st.dataframe(_df, use_container_width=True, hide_index=True)
            else:
                st.caption("No season stats available yet.")
