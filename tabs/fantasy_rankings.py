# tabs/fantasy_rankings.py
# Phase 1 Fantasy Rankings UI — initial Beta release, wired into
# tabs/fantasy_tools.py's "Section" selector. Sportsbook-market-only
# translation into fantasy points (core/nfl_fantasy_rankings.py); no
# methodology lives in this file, only presentation.
import pandas as pd
import streamlit as st

from core.data_sources import get_date_window, infer_current_week_index
from core.nfl_prop_data_sources import get_upcoming_prop_event_ids
from core.nfl_prop_market_config import PROP_MARKETS
from core.nfl_fantasy_rankings import (
    build_fantasy_rankings, SCORING_OPTIONS, RANKING_POSITIONS, _REQUIRED_MARKETS,
)
from core.nfl_provider_diagnostics import run_provider_diagnostics, ESTIMATED_MAX_CREDITS

_BASE_COLS = ["Rank", "Player", "Team", "Pos", "FVB Fantasy Pts"]


def _tight_label(text: str):
    st.markdown(
        f"<div style='font-size:0.78rem;opacity:0.65;margin:0 0 1px 0;line-height:1'>{text}</div>",
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=300, show_spinner=False)
def _load_rankings(_supabase, event_ids: tuple, window_start, window_end, scoring: str) -> dict:
    return build_fantasy_rankings(_supabase, list(event_ids), window_start, window_end, scoring)


@st.cache_data(ttl=600, show_spinner="Querying Odds API provider diagnostics...")
def _cached_run_provider_diagnostics(_supabase, event_id: str) -> dict:
    # Cached for 10 minutes so repeated button clicks (or unrelated page
    # reruns once a result already exists) don't re-spend API credits --
    # this is the ONLY place in this file that calls
    # core.nfl_provider_diagnostics.run_provider_diagnostics, and it is
    # only ever invoked from inside the button-click branch below, never
    # on a bare render().
    return run_provider_diagnostics(_supabase, event_id)


def _detail_lines(detail_for_player: dict) -> list[str]:
    lines = []
    for label, d in detail_for_player.items():
        if d.get("unavailable"):
            # Anytime TD is optional/additive for RB/WR/TE -- this is not
            # a sportsbook-implied zero-TD probability, just "we don't
            # currently have a valid 2+ book consensus for this player."
            lines.append(f"**{label}:** TD market unavailable — not included in this player's score")
            continue
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
    st.markdown(
        "## Fantasy Rankings "
        "<span style='font-size:0.62rem;font-weight:600;letter-spacing:0.04em;"
        "opacity:0.65;border:1px solid currentColor;border-radius:4px;"
        "padding:1px 6px;vertical-align:middle;margin-left:4px'>BETA</span>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div style='opacity:0.7;font-size:0.95rem;margin:0 0 10px 0'>"
        "FVB Fantasy Rankings translate sportsbook markets into fantasy points — "
        "not an independent player-performance projection. Every number below comes "
        "directly from a sportsbook-weighted consensus market. For RB/WR/TE, Anytime TD "
        "is included when a sportsbook consensus is available and simply omitted (never "
        "assumed to be zero) when it isn't."
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
            f"No {_position} rankings are available yet for {caption_label}. "
            "Rankings populate once enough sportsbooks have posted the markets "
            "needed to fairly score a player — check back as more lines are posted."
        )
    else:
        stat_cols = [c for c in df.columns if c not in _BASE_COLS and not c.startswith("_")]
        display_cols = [c for c in _BASE_COLS if c in df.columns] + stat_cols
        _display_df = df[display_cols].copy()
        _td_col = "Anytime TD (λ pts)"
        if _td_col in _display_df.columns:
            # Format to a uniform string column ("13.09" or "--") rather
            # than leaving it numeric with NaN gaps -- an explicit "--"
            # reads clearly as "not available" instead of a blank cell
            # that could be misread as a rendering gap.
            _display_df[_td_col] = _display_df[_td_col].apply(lambda v: f"{v:.2f}" if pd.notna(v) else "—")
        st.dataframe(
            _display_df, use_container_width=True, hide_index=True,
            height=min(760, 46 + 35 * len(df)),
            column_config={
                "Rank": st.column_config.NumberColumn("RANK", width="small"),
                "Player": st.column_config.TextColumn("PLAYER", width="medium"),
                "Team": st.column_config.TextColumn("TEAM", width="small"),
                "Pos": st.column_config.TextColumn("POS", width="small"),
                "FVB Fantasy Pts": st.column_config.NumberColumn("FVB FANTASY PTS", width="small", format="%.2f"),
                _td_col: st.column_config.TextColumn(
                    "ANYTIME TD (λ PTS)", width="small",
                    help="Optional/additive. \"—\" means no valid 2+ sportsbook Anytime TD consensus "
                         "is currently available for this player — not a sportsbook-implied zero.",
                ),
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
            f"Not yet ranked due to limited sportsbook coverage: {', '.join(_parts)}. "
            "These players will appear once enough sportsbooks post the required markets."
        )
    st.caption(
        "FVB Fantasy Rankings translate sportsbook markets into fantasy points. "
        "They are not an FVB fantasy projection model."
    )

    _render_coverage_diagnostics(result)
    _render_provider_diagnostics(supabase, event_ids)


def _render_coverage_diagnostics(result: dict):
    """Temporary/internal validation view — surfaces the SAME data already
    loaded for the rankings above (result["considered"]/["excluded_detail"]/
    ["market_coverage"]/["anytime_td_diag"], all computed once inside
    build_fantasy_rankings) so coverage gaps are visible directly on the
    live page instead of requiring a separate Render-shell diagnostic run.
    No additional Supabase or Odds API calls; no methodology/eligibility
    logic here — this only reads and displays already-computed results."""
    with st.expander("Coverage Diagnostics", expanded=False):
        st.caption(
            "Internal validation view of the sportsbook-market coverage behind the "
            "rankings above, from the same data already loaded on this page."
        )

        considered = result.get("considered", {})
        excluded = result.get("excluded", {})
        st.markdown("**Per-position coverage**")
        st.dataframe(
            pd.DataFrame([
                {"Position": pos, "Players considered": considered.get(pos, 0),
                 "Qualifying": len(result.get(pos, pd.DataFrame())), "Excluded": excluded.get(pos, 0)}
                for pos in RANKING_POSITIONS
            ]),
            use_container_width=True, hide_index=True,
        )

        st.markdown("**Required-market coverage** (players reaching a 2+ sportsbook weighted consensus)")
        market_coverage = result.get("market_coverage", {})
        all_required_markets = sorted({m for pos in RANKING_POSITIONS for m in _REQUIRED_MARKETS[pos]})
        coverage_rows = []
        for pos in RANKING_POSITIONS:
            row = {"Position": pos}
            for mkt in all_required_markets:
                label = PROP_MARKETS[mkt].market_label
                # Cast to str: this column otherwise mixes int (a required
                # market's player count) and "—" (not required for this
                # position) within one column, which pyarrow can't convert
                # for st.dataframe -- a real bug caught by testing this
                # against a mixed-type fixture, not a style preference.
                row[label] = str(market_coverage.get(mkt, 0)) if mkt in _REQUIRED_MARKETS[pos] else "—"
            coverage_rows.append(row)
        st.dataframe(pd.DataFrame(coverage_rows), use_container_width=True, hide_index=True)

        st.markdown("**Anytime TD status** (optional/additive for RB/WR/TE — does not exclude a player when unavailable)")
        td_diag = result.get("anytime_td_diag", {})
        st.markdown(
            f"- Raw Anytime TD line rows present: **{td_diag.get('raw_rows', 0)}**\n"
            f"- Yes/No sides present: **{td_diag.get('sides_present', [])}**\n"
            f"- Players reaching a 2+ sportsbook fair-probability consensus: "
            f"**{td_diag.get('players_2plus_books', 0)}**"
        )

        st.markdown("**Excluded players** (missing one or more required markets)")
        excluded_detail = result.get("excluded_detail", {})
        excl_rows = []
        for pos in RANKING_POSITIONS:
            for e in excluded_detail.get(pos, []):
                excl_rows.append({
                    "Player": e.get("display_name", ""), "Team": e.get("team") or "—",
                    "Position": e.get("position", pos), "Missing required market(s)": ", ".join(e.get("missing", [])),
                })
        if excl_rows:
            st.dataframe(pd.DataFrame(excl_rows), use_container_width=True, hide_index=True,
                         height=min(500, 46 + 35 * len(excl_rows)))
        else:
            st.caption("No excluded players for the current selection.")


def _render_provider_diagnostics(supabase, event_ids: list):
    """TEMPORARY, internal-only diagnostics for verifying The Odds API's
    actual behavior for NFL player-prop markets (Anytime TD Yes/No
    coverage, regions=us2, Kalshi/us_ex) -- see
    core/nfl_provider_diagnostics.py for the underlying checks. Makes
    live Odds API calls ONLY when the button below is clicked -- never on
    a bare page load or unrelated rerun -- and the fetch itself is
    wrapped in @st.cache_data(ttl=600) so repeated clicks within 10
    minutes reuse the cached result instead of re-spending API credits.
    Clearly separated from the customer-facing rankings above: its own
    collapsed expander, explicitly labeled internal, no methodology or
    eligibility logic anywhere in this function."""
    with st.expander("Provider Diagnostics", expanded=False):
        st.caption(
            "Internal-only: verifies The Odds API's actual behavior for NFL player-prop "
            "markets (Anytime TD Yes/No coverage, regions=us2, Kalshi). Not part of the "
            "customer-facing rankings above -- for engineering validation."
        )
        if not event_ids:
            st.caption("No upcoming event available to test right now.")
            return

        target_event_id = event_ids[0]
        st.caption(f"Event to test: `{target_event_id}`")
        st.caption(
            f"This diagnostic may use up to ~{ESTIMATED_MAX_CREDITS} API credits per run "
            "(4 live provider requests). Results are cached for 10 minutes -- repeated clicks "
            "within that window will not re-spend credits."
        )

        if st.button("Run Provider Diagnostics", key="pd_run_btn"):
            st.session_state["pd_result"] = _cached_run_provider_diagnostics(supabase, target_event_id)
            st.session_state["pd_result_event_id"] = target_event_id

        result = st.session_state.get("pd_result")
        if result is None:
            st.caption("Click the button above to run these checks against the live provider.")
            return
        if st.session_state.get("pd_result_event_id") != target_event_id:
            st.caption(
                "Note: the cached result below is for a previously-tested event, not the one "
                "currently in view. Click Run again to refresh for this event."
            )

        if result.get("errors"):
            st.error(
                "Some diagnostic requests did not complete:\n\n"
                + "\n".join(f"- {e}" for e in result["errors"])
            )

        ev = result.get("event", {})
        st.markdown(
            f"**Event tested:** {ev.get('away_team') or '?'} @ {ev.get('home_team') or '?'}  "
            f"(`{ev.get('event_id') or target_event_id}`)"
        )

        st.markdown("**Anytime TD — US**")
        _us_df = result.get("us_breakdown")
        if _us_df is not None and not _us_df.empty:
            st.dataframe(_us_df, use_container_width=True, hide_index=True)
        else:
            st.caption("No bookmaker returned player_anytime_td under regions=us.")

        st.markdown("**Anytime TD — US2**")
        _us2_df = result.get("us2_breakdown")
        if _us2_df is not None and not _us2_df.empty:
            st.dataframe(_us2_df, use_container_width=True, hide_index=True)
        else:
            st.caption("No bookmaker returned player_anytime_td under regions=us2.")
        _us2_summary = result.get("us2_summary", {})
        st.caption(
            f"us2 total players: {_us2_summary.get('total_players', 0)} — "
            f"new vs. us: {len(_us2_summary.get('new_players_vs_us', []))} "
            f"{_us2_summary.get('new_players_vs_us', [])[:10]} — "
            f"bookmakers unique to us2: {_us2_summary.get('unique_bookmakers', [])}"
        )

        st.markdown("**Kalshi coverage** (all 7 prop markets)")
        st.caption(
            f"Kalshi present under regions=us_ex: {'Yes' if result.get('kalshi_present_usex') else 'No'} — "
            f"Kalshi present via bookmakers=kalshi (explicit): "
            f"{'Yes' if result.get('kalshi_present_explicit') else 'No'}"
        )
        _kalshi_usex_df = result.get("kalshi_usex")
        if _kalshi_usex_df is not None and not _kalshi_usex_df.empty:
            st.dataframe(_kalshi_usex_df, use_container_width=True, hide_index=True)
        _kalshi_explicit_df = result.get("kalshi_explicit")
        if _kalshi_explicit_df is not None and not _kalshi_explicit_df.empty:
            st.dataframe(_kalshi_explicit_df, use_container_width=True, hide_index=True)

        st.markdown("**Two-sided coverage summary** (Anytime TD, regions=us) — the key decision metric")
        _viability = result.get("two_sided_viability", {})
        st.dataframe(
            pd.DataFrame([{
                "Total players": _viability.get("total_players", 0),
                "1+ two-sided books": _viability.get("n_1plus", 0),
                "2+ two-sided books": _viability.get("n_2plus", 0),
                "3+ two-sided books": _viability.get("n_3plus", 0),
            }]),
            use_container_width=True, hide_index=True,
        )

        st.markdown("**Consensus drop check**")
        _trace = result.get("drop_point_trace")
        if _trace is None:
            st.caption(
                "No Yes-only player/book example found in this response — every Yes row had a "
                "matching No row from the same book."
            )
        else:
            st.dataframe(
                pd.DataFrame([{
                    "Player": _trace.get("player"), "Bookmaker": _trace.get("book"),
                    "Yes price": _trace.get("yes_price"), "No price": _trace.get("no_price"),
                    "Implied Yes": _trace.get("implied_yes"), "Implied No": _trace.get("implied_no"),
                    "Survives both-sides gate": "Yes" if _trace.get("survives_dropna") else "No — DROPPED",
                    "In final consensus": "Yes" if _trace.get("in_final_consensus") else "No",
                }]),
                use_container_width=True, hide_index=True,
            )

        st.caption(
            f"API credit note: this run makes 4 live Odds API requests, up to ~{ESTIMATED_MAX_CREDITS} "
            "credits worst case, only when the button above is clicked, cached for 10 minutes after."
        )
