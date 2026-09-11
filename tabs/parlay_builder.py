# tabs/parlay_builder.py
from decimal import Decimal, ROUND_HALF_UP

import pandas as pd
import streamlit as st

from core.odds_math import (
    american_to_decimal, american_to_implied_prob, expected_value_pct, fmt_ev, fmt_odds,
    parse_iso_dt_utc, EASTERN,
)
from core.pipeline import MARKETS, run_market_pipeline
from core.data_sources import fetch_market_lines, filter_by_window, get_date_window, infer_current_week_index
from core.nfl_prop_market_config import PROP_MARKETS, normalize_player_key
from core.nfl_prop_pipeline import run_prop_market_pipeline, build_prop_books_df
from core.nfl_prop_data_sources import fetch_prop_market_lines, get_upcoming_prop_event_ids

# Same team-abbreviation map and ESPN CDN logo URL pattern already used by
# Sportsbook Screener / Matchup Center / Market Movers — reused as-is.
NFL_TEAM_ABBR = {
    "Arizona Cardinals": "ari", "Atlanta Falcons": "atl", "Baltimore Ravens": "bal",
    "Buffalo Bills": "buf", "Carolina Panthers": "car", "Chicago Bears": "chi",
    "Cincinnati Bengals": "cin", "Cleveland Browns": "cle", "Dallas Cowboys": "dal",
    "Denver Broncos": "den", "Detroit Lions": "det", "Green Bay Packers": "gb",
    "Houston Texans": "hou", "Indianapolis Colts": "ind", "Jacksonville Jaguars": "jax",
    "Kansas City Chiefs": "kc", "Las Vegas Raiders": "lv", "Los Angeles Chargers": "lac",
    "Los Angeles Rams": "lar", "Miami Dolphins": "mia", "Minnesota Vikings": "min",
    "New England Patriots": "ne", "New Orleans Saints": "no", "New York Giants": "nyg",
    "New York Jets": "nyj", "Philadelphia Eagles": "phi", "Pittsburgh Steelers": "pit",
    "San Francisco 49ers": "sf", "Seattle Seahawks": "sea", "Tampa Bay Buccaneers": "tb",
    "Tennessee Titans": "ten", "Washington Commanders": "wsh",
}


def _logo_url(team_name: str) -> str | None:
    abbr = NFL_TEAM_ABBR.get(team_name)
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png" if abbr else None


def _short_team(team_name) -> str:
    """Last word of a full team name (e.g. 'Green Bay Packers' -> 'Packers')
    — every NFL full team name already carried in "Game" ends in the
    mascot name, so this needs no lookup table."""
    if not team_name:
        return "—"
    return team_name.split()[-1]


def _prop_bet_label(leg) -> str:
    """Prop leg label, e.g. 'McCaffrey Over · Rushing Yards' -- short
    player surname (matching the existing short-label convention already
    used for game-market legs), full Over/Under word, full market name
    (no new abbreviation table). No line: a Player Prop leg is now
    Player + Market + Side only -- each sportsbook uses its own actual
    posted line for that side, so there is no longer a single line this
    label could show without implying a specific threshold that may not
    match every book. Used both as the "Bet" label in Current Parlay and
    as the Remove-selectbox entry for prop legs."""
    player = leg.get("Player", "")
    last_name = player.split()[-1] if player else "—"
    market = leg.get("Market", "")
    side = leg.get("Side", "")
    return f"{last_name} {side} · {market}".strip()


# Unit word for the informational average-line display below, per Phase 1
# market (core/nfl_prop_market_config.py::PROP_MARKETS) -- "yards" would be
# wrong for a count stat like Passing TDs/Receptions, so this is an
# explicit small map rather than a blanket suffix. A market with no entry
# here simply displays with no unit word.
_PROP_AVG_UNIT = {
    "Passing Yards": "yards", "Rushing Yards": "yards", "Receiving Yards": "yards",
    "Passing TDs": "TDs", "Receptions": "receptions",
}


def _fmt_avg_line(value: float) -> str:
    """Always exactly 1 decimal place, standard round-half-up (not
    Python's default round-half-to-even, which would render 59.25 as
    "59.2" instead of the expected "59.3") -- via Decimal so the
    rounding is exact regardless of the value's binary float
    representation. Trailing zero always preserved (e.g. 40 -> "40.0")."""
    return str(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _prop_fair_prob_for_line(df_props: pd.DataFrame, leg, line_value) -> float | None:
    """
    Opportunistic EV lookup for ONE sportsbook's own specific prop line:
    looks up the already-computed consensus fair probability (_fair_raw)
    for this EXACT (Game, Market, Player, Side, line_value) combination
    in df_props, returning None if no such row exists -- i.e. if that
    specific line never cleared the existing MIN_BOOKS_FOR_PROP_CONSENSUS
    coverage gate (an expected, common case now that different books can
    each use their own unique threshold for the same Player+Market+Side
    leg). Never searches nearby lines, averages, or otherwise substitutes
    a different line's probability -- P(player over X) is a genuinely
    different probability from P(player over Y) for X != Y, and this
    must never silently blur the two together.
    """
    if df_props.empty or line_value is None or pd.isna(line_value):
        return None
    try:
        _line_str = f"{float(line_value):g}"
    except (TypeError, ValueError):
        return None
    match = df_props[
        (df_props["Game"] == leg.get("Game")) &
        (df_props["Market"] == leg.get("Market")) &
        (df_props["Player"] == leg.get("Player")) &
        (df_props["Side"] == leg.get("Side")) &
        (df_props["Line"] == _line_str)
    ]
    if match.empty:
        return None
    fp = match.iloc[0].get("_fair_raw")
    return float(fp) if pd.notna(fp) else None


def _select_representative_line(candidate_rows: pd.DataFrame, tiebreak: str = "lower", group_key_fn=None) -> pd.DataFrame:
    """
    Selects ONE exact line's rows from an already-priced set of candidate
    rows that differ only by exact "Line" value (one Player+Market's rows
    for Player Props; one game's Spread rows; one game's Total rows),
    per the shared representative-line rule used across Player Props,
    Spread, and Total:
      1. highest mi_num_books (most sportsbook coverage AT THAT EXACT
         LINE -- already computed per-line, unmodified, by
         core/pipeline.py::build_market_intelligence),
      2. tie-broken by higher mi_num_anchors (more anchor-book
         agreement -- reuses the app's existing ANCHOR_BOOKS definition,
         not a new signal),
      3. final deterministic tie-break, controlled by `tiebreak`:
         - "lower": the lower numeric line value (Total, Player Props).
         - "abs": the smallest absolute line value (Spread). Spread
           lines are signed toward a specific team, so "lower" would
           silently bias the tie-break toward the favorite being a
           bigger favorite; absolute value carries no such directional
           bias.

    group_key_fn maps a row's own "Line" string to the value used to
    group candidate lines together -- defaults to the raw string itself
    (Total, Player Props, where every side of one threshold already
    shares one identical Line string). Spread passes abs(float(line)):
    core/pipeline.py::build_display_rows splits one underlying spread
    threshold into a home row (e.g. "-3") and an away row (e.g. "+3")
    with OPPOSITE-SIGNED Line strings for the SAME threshold -- without
    this, home and away would be miscounted as two separate
    representative-line candidates instead of the two sides of one.

    Pure selection over rows already devigged/consensus-priced upstream
    -- never averages, interpolates, or normalizes across lines, and can
    never produce a line value that wasn't already an existing priced
    row.
    """
    if group_key_fn is None:
        group_key_fn = lambda l: l

    _rows = candidate_rows.dropna(subset=["Line"]).copy()
    if _rows.empty:
        return candidate_rows.iloc[0:0]
    _rows["_group_key"] = _rows["Line"].apply(group_key_fn)

    def _group_sort_key(group_key):
        _grp = _rows[_rows["_group_key"] == group_key]
        _first = _grp.iloc[0]
        _num_books = _first.get("mi_num_books")
        _num_anchors = _first.get("mi_num_anchors")
        try:
            _line_num = float(_first["Line"])
        except (TypeError, ValueError):
            _line_num = float("inf")
        _tiebreak_num = abs(_line_num) if tiebreak == "abs" else _line_num
        return (
            -(_num_books if pd.notna(_num_books) else -1),
            -(_num_anchors if pd.notna(_num_anchors) else -1),
            _tiebreak_num,
        )

    _distinct_groups = _rows["_group_key"].unique()
    _best_group = min(_distinct_groups, key=_group_sort_key)
    return _rows[_rows["_group_key"] == _best_group].drop(columns=["_group_key"])


def _leg_short_label(leg) -> str:
    """Compact single-string identifier for one parlay leg, e.g.
    'Packers ML @ Broncos', 'Dolphins +2.5 @ Giants',
    'Over 44.5 Bengals @ Bears', or (prop legs) 'Jefferson Receiving
    Yards O79.5'."""
    if leg.get("Type") == "prop":
        return _prop_bet_label(leg)

    game = leg.get("Game", "")
    teams = game.split(" vs ") if isinstance(game, str) else []
    home_team = teams[0] if len(teams) == 2 else None
    away_team = teams[1] if len(teams) == 2 else None
    market = leg.get("Market", "")
    pick = leg.get("Pick", "—")
    line = leg.get("Line")

    if market == "Total":
        line_part = f" {line}" if line else ""
        return f"{pick}{line_part} {_short_team(away_team)} @ {_short_team(home_team)}"

    opponent = away_team if pick == home_team else home_team
    suffix = "ML" if market == "Moneyline" else (str(line) if line else "")
    return f"{_short_team(pick)} {suffix} @ {_short_team(opponent)}".replace("  ", " ").strip()


def _leg_game_label(leg) -> str:
    """Compact matchup only, e.g. 'Packers @ Broncos' (away @ home, short
    names) — for the Current Parlay table's separate Game column."""
    game = leg.get("Game", "")
    teams = game.split(" vs ") if isinstance(game, str) else []
    if len(teams) != 2:
        return game
    home_team, away_team = teams
    return f"{_short_team(away_team)} @ {_short_team(home_team)}"


def _leg_pipeline_row(leg, df_all: pd.DataFrame):
    """Looks up this leg's own row in the already-fetched FVM pipeline
    output — no new calculation, no new fetch. Matches on (Game, Market,
    Pick) and, for spread/total, also on Line (a game can have more than
    one alternate line). Used to read both the consensus fair probability
    (_fair_raw, for Parlay Comparison EV%) and the Best Odds (for the
    Current Parlay Bet label) from a single lookup."""
    if df_all.empty:
        return None
    match = df_all[
        (df_all["Game"] == leg.get("Game")) &
        (df_all["Market"] == leg.get("Market")) &
        (df_all["Pick"] == leg.get("Pick"))
    ]
    if leg.get("Market") != "Moneyline" and "Line" in match.columns:
        match = match[match["Line"] == leg.get("Line")]
    return match.iloc[0] if not match.empty else None


def _leg_fair_prob(leg, df_all: pd.DataFrame):
    row = _leg_pipeline_row(leg, df_all)
    if row is None:
        return None
    fp = row.get("_fair_raw")
    return float(fp) if pd.notna(fp) else None


def _leg_best_odds(leg, df_all: pd.DataFrame):
    row = _leg_pipeline_row(leg, df_all)
    if row is None:
        return None
    odds = row.get("Best Odds")
    return int(odds) if pd.notna(odds) else None


def _leg_bet_label(leg) -> str:
    """Just the selected side, e.g. 'Packers', 'Dolphins +2.5',
    'Under 37.5' — for the Current Parlay table's Bet column. The price
    itself lives in the separate Odds column, not embedded here.
    leg["Line"] is already the selected side's own correctly-signed line
    for spreads (build_display_rows pairs each row's "Line" with that
    row's own Pick — see the away-side spread sign fix, commit b2242a5),
    so no extra sign derivation is needed here. Prop legs use the same
    compact label as everywhere else (_prop_bet_label) rather than the
    team-name logic below, which doesn't apply to a player pick."""
    if leg.get("Type") == "prop":
        return _prop_bet_label(leg)
    market = leg.get("Market", "")
    pick = leg.get("Pick", "—")
    line = leg.get("Line")
    if market == "Moneyline":
        return _short_team(pick)
    if market == "Total":
        return f"{pick} {line}" if line else pick
    return f"{_short_team(pick)} {line}" if line else _short_team(pick)


def _leg_odds_label(leg, df_all: pd.DataFrame, odds_format: str) -> str:
    """The selected side's current odds, in the currently selected Odds
    Format — for the Current Parlay table's separate Odds column. Looked
    up fresh from the already-fetched pipeline output (Best Odds), same
    source Browse Games uses for the same pick.

    Player Props are line-agnostic now (Player + Market + Side, not a
    specific threshold) -- each sportsbook uses its own actual posted
    line, so there is no longer a single "the odds" to show before
    Compare Parlay Odds resolves each book's own line/price explicitly.
    """
    if leg.get("Type") == "prop":
        return "—"
    odds = _leg_best_odds(leg, df_all)
    return _fmt_odds_in_format(odds, odds_format) if odds is not None else "—"


def _fmt_odds_in_format(american_price, fmt: str) -> str:
    """Same small display-only formatting pattern already used in
    tabs/fair_value_model.py::_fmt_best_odds — no new conversion
    methodology, just American/Decimal/Implied % presentation of an
    already-computed American price."""
    if american_price is None:
        return "—"
    price = int(american_price)
    if fmt == "American":
        return fmt_odds(price)
    elif fmt == "Decimal":
        return f"{american_to_decimal(price):.2f}x"
    else:
        p = american_to_implied_prob(price)
        return f"{p * 100:.1f}%" if p else "—"


def _fragment_rerun() -> None:
    """Rerun after a session_state mutation so the already-updated state is
    reflected on the very next pass, instead of only becoming visible on
    some later, unrelated interaction. Scoped to the fragment so an Add/
    Remove click doesn't force every other tab's data loading to re-run —
    matches the reason this whole body is wrapped in @st.fragment. Falls
    back to a plain rerun on older Streamlit versions without fragment
    scoping."""
    try:
        st.rerun(scope="fragment")
    except TypeError:
        st.rerun()


@st.cache_data(ttl=300, show_spinner=False)
def _load_parlay_builder_markets(_supabase, sport_keys: frozenset, window_start, window_end, bankroll: float, kelly: float) -> dict[str, pd.DataFrame]:
    """
    Pure computation: fetch + price all 3 game markets, reused by Current
    Parlay, Compare Parlay Odds, and Browse Games. Wrapped in @st.fragment
    below via _parlay_builder_body, but a fragment only scopes reruns
    triggered by a widget INSIDE it — a rerun triggered elsewhere in the
    app (e.g. a Prop Research dropdown) still re-executes this tab's
    render() top to bottom, including this fragment call. Without this
    cache, the full 3-market devig/consensus/EV pipeline re-ran on every
    one of those unrelated reruns for every signed-in user. Same pattern
    as tabs/market_movers.py::_load_market_movers_data.
    """
    _display: dict[str, pd.DataFrame] = {}
    for _mkt_key, _mkt_cfg in MARKETS.items():
        _raw_pb, _ = fetch_market_lines(_supabase, sport_keys, _mkt_cfg.db_market_key)
        _raw_pb = filter_by_window(_raw_pb, window_start, window_end)
        _display[_mkt_key] = run_market_pipeline(
            raw_lines=_raw_pb, cfg=_mkt_cfg, bankroll=bankroll, kelly=kelly,
            min_ev=0.0, min_fair_pct=0.0, show_all=True,
        )
    return _display


@st.cache_data(ttl=300, show_spinner=False)
def _load_parlay_builder_props(_supabase, event_ids: tuple, window_start, window_end, bankroll: float, kelly: float) -> pd.DataFrame:
    """
    Pure computation: fetch + price every Phase-1 FVB player-prop market
    for the Parlay Builder's Player Props mode, reusing the exact same
    core/nfl_prop_pipeline.py engine (devig, consensus, best-price, EV%,
    Kelly -- all imported unmodified from core/pipeline.py inside that
    module) that Fair Value Model's own Player Props view already runs.
    Deliberately NOT core/lineup_data.py's fetch_player_props_for_event
    path -- that's Prop Research's lightweight median-line lookup, which
    has no fair-value pricing at all and isn't the "first-class FVB
    market category" this feature is meant to reuse.

    Cached the same way _load_parlay_builder_markets above is, for the
    same reason: st.tabs() re-executes this tab's render() on every
    unrelated app-wide rerun, and without this the full prop
    devig/consensus/EV pipeline would re-run on every one of those.
    """
    _all_prop_display = []
    for _mkt_key, _mkt_cfg in PROP_MARKETS.items():
        if not event_ids:
            continue
        _raw_props = fetch_prop_market_lines(_supabase, list(event_ids), _mkt_key)
        _raw_props = filter_by_window(_raw_props, window_start, window_end)
        _df = run_prop_market_pipeline(
            raw_lines=_raw_props, cfg=_mkt_cfg, bankroll=bankroll, kelly=kelly,
            min_ev=0.0, min_fair_pct=0.0, show_all=True,
        )
        if not _df.empty:
            _all_prop_display.append(_df)
    return pd.concat(_all_prop_display, ignore_index=True) if _all_prop_display else pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def _load_parlay_builder_prop_books(_supabase, event_ids: tuple, window_start, window_end) -> dict[str, pd.DataFrame]:
    """
    Pure computation: fetch + pivot (build_prop_books_df, unmodified --
    NOT run through run_prop_market_pipeline's devig/consensus/coverage
    gate) every Phase-1 FVB player-prop market's raw per-book lines.

    Player Props legs are now line-agnostic (Player + Market + Side, not
    a specific threshold) -- each sportsbook uses its own actual posted
    line for that side, so both browsing (which players/markets/sides
    are actually offered) and Compare Parlay Odds (each book's own line
    + price) must read raw per-book data, not the consensus-gated FVB
    pipeline output (df_props / run_prop_market_pipeline). That pipeline
    drops any line with fewer than MIN_BOOKS_FOR_PROP_CONSENSUS books --
    exactly the case where three different books each post their own
    unique line for the same player/market (58.5, 59.5, 60.5, one book
    apiece) and none of them would individually clear the gate, even
    though each is a perfectly real, bettable line at that book.

    df_props (still loaded separately, unchanged) is NOT replaced by
    this -- it remains the source for an opportunistic EV%/fair-
    probability lookup when a specific book's own line happens to also
    be a consensus-priced one (see _prop_fair_prob_for_line above).
    """
    _books: dict[str, pd.DataFrame] = {}
    for _mkt_key, _mkt_cfg in PROP_MARKETS.items():
        if not event_ids:
            _books[_mkt_cfg.market_label] = pd.DataFrame()
            continue
        _raw = fetch_prop_market_lines(_supabase, list(event_ids), _mkt_key)
        _raw = filter_by_window(_raw, window_start, window_end)
        _books[_mkt_cfg.market_label] = build_prop_books_df(_raw, _mkt_cfg)
    return _books


def render(supabase, now_utc, eff_bankroll, eff_kelly, authed):
    if not authed:
        st.warning("Sign in to use the Parlay Builder.")
        return

    st.subheader("NFL Parlay Builder")
    _pb_col1, _pb_col2 = st.columns(2)
    with _pb_col1:
        stake = st.number_input("Stake ($)", min_value=1.0, value=10.0, step=1.0, key="pb_stake")
    with _pb_col2:
        _odds_format = st.selectbox(
            "Odds Format", ["American", "Decimal", "Implied %"], key="pb_odds_format",
        )
    st.session_state.setdefault("pb_parlay_legs", [])

    _wk = infer_current_week_index(now_utc)
    _week_label = "NFL Preseason" if _wk == 0 else f"NFL Week {_wk}"
    _window_choice = st.selectbox(
        "Date Range", ["Today", _week_label, "Next 7 Days"], index=1, key="pb_window_choice",
    )
    window_start, window_end, sport_keys, caption_label = get_date_window(now_utc, _window_choice)

    @st.fragment
    def _parlay_builder_body():
        # Single fetch reused by Current Parlay (Bet-label odds lookup),
        # Compare Parlay Odds (fair-probability lookup for EV%), and
        # Browse Games — no new fetch is introduced by any of these three
        # consumers needing the same already-fetched pipeline output.
        _pb_display = _load_parlay_builder_markets(
            supabase, frozenset(sport_keys), window_start, window_end, eff_bankroll, eff_kelly,
        )
        df_all = (
            pd.concat([df for df in _pb_display.values() if not df.empty], ignore_index=True)
            if any(not df.empty for df in _pb_display.values())
            else pd.DataFrame()
        )

        # Player Props: same fetch-once-reuse-everywhere shape as game
        # markets above. Loaded unconditionally (not gated on the
        # Game Markets/Player Props mode toggle below) because a prop leg
        # already in the parlay still needs its odds looked up correctly
        # in Current Parlay even while the user is viewing Game Markets
        # mode -- both @st.cache_data, so this costs nothing extra beyond
        # the first load within the 300s TTL.
        _prop_event_ids = get_upcoming_prop_event_ids(supabase, window_start.isoformat(), window_end.isoformat())
        df_props = _load_parlay_builder_props(
            supabase, tuple(_prop_event_ids), window_start, window_end, eff_bankroll, eff_kelly,
        )
        # Raw (ungated) per-book prop pivot -- the source of truth for
        # Player Props browsing and Compare Parlay Odds now that a prop
        # leg is line-agnostic; see _load_parlay_builder_prop_books'
        # docstring for why this can't be df_props.
        _prop_books_pb = _load_parlay_builder_prop_books(
            supabase, tuple(_prop_event_ids), window_start, window_end,
        )
        # Game-market and prop display rows already share the same Game/
        # Market/Pick/Line/_fair_raw/Best Odds/mi_book_table column shape
        # (core/nfl_prop_pipeline.py::build_prop_display_rows was written
        # to mirror core/pipeline.py::build_display_rows exactly for this
        # reason) -- concatenating them lets the existing Game/Market/
        # Pick/Line-keyed lookup helpers below (_leg_pipeline_row,
        # _leg_fair_prob, _leg_best_odds, _leg_odds_label) work for BOTH
        # leg types with no changes to those functions themselves.
        df_all_combined = (
            pd.concat([d for d in (df_all, df_props) if not d.empty], ignore_index=True)
            if not df_all.empty or not df_props.empty
            else pd.DataFrame()
        )

        # Same-game disclosure: pure labeling, no change to the combined
        # fair-probability/EV math below, which still treats every leg as
        # independent exactly as it did before Player Props existed.
        _game_leg_counts: dict = {}
        for _l in st.session_state.pb_parlay_legs:
            _g = _l.get("Game")
            if _g:
                _game_leg_counts[_g] = _game_leg_counts.get(_g, 0) + 1
        if any(_c >= 2 for _c in _game_leg_counts.values()):
            st.caption("Same-game legs detected · Parlay EV assumes independent outcomes.")

        st.markdown("### Current Parlay")
        if not st.session_state.pb_parlay_legs:
            st.info("Add at least two legs by clicking Add on any pick below.")
        else:
            _leg_labels = [_leg_short_label(l) for l in st.session_state.pb_parlay_legs]
            st.dataframe(
                pd.DataFrame([
                    {
                        "Market": l.get("Market", "—"),
                        "Game": _leg_game_label(l),
                        "Bet": _leg_bet_label(l),
                        "Odds": _leg_odds_label(l, df_all_combined, _odds_format),
                    }
                    for l in st.session_state.pb_parlay_legs
                ]),
                use_container_width=True, hide_index=True,
            )

            # st.dataframe is read-only and can't host a per-row button, so
            # removal uses the smallest native workaround: a compact table
            # for display, paired with an adjacent selectbox + Remove
            # button rather than trying to put an interactive control
            # inside a table cell.
            _num_legs = len(st.session_state.pb_parlay_legs)
            if st.session_state.get("pb_remove_idx", 0) >= _num_legs:
                st.session_state["pb_remove_idx"] = 0
            _rm_c1, _rm_c2 = st.columns([3, 1])
            with _rm_c1:
                _remove_idx = st.selectbox(
                    "Remove a leg", options=list(range(_num_legs)),
                    format_func=lambda i: _leg_labels[i],
                    key="pb_remove_idx", label_visibility="collapsed",
                )
            with _rm_c2:
                if st.button("Remove", key="pb_remove_btn", use_container_width=True):
                    st.session_state.pb_parlay_legs.pop(_remove_idx)
                    _fragment_rerun()

            colA, colB = st.columns(2)
            compare = colA.button("Compare Parlay Odds", use_container_width=True, key="pb_compare")
            if colB.button("Clear All Legs", use_container_width=True,
                           disabled=not st.session_state.pb_parlay_legs, key="pb_clear_all"):
                st.session_state.pb_parlay_legs = []
                _fragment_rerun()

            if compare and len(st.session_state.pb_parlay_legs) >= 2:
                _markets_pb = {}
                for _mkt_key, _mkt_cfg in MARKETS.items():
                    _raw_p, _ = fetch_market_lines(supabase, sport_keys, _mkt_cfg.db_market_key)
                    _raw_p = filter_by_window(_raw_p, window_start, window_end)
                    from core.pipeline import build_books_df
                    _markets_pb[_mkt_cfg.market_label] = build_books_df(_raw_p, _mkt_cfg)

                # Prop books come from the raw per-book pivot loaded above
                # (_prop_books_pb) -- a Player Prop leg is now line-
                # agnostic (Player + Market + Side only), so eligibility
                # can no longer be scoped through a single consensus-
                # priced row's mi_book_table (that one exact-line row no
                # longer represents "the" leg). Any book with a non-null
                # Over/Under price for this player/market, at ANY line,
                # can carry the leg -- using ITS OWN line and price,
                # never another book's.
                _prop_books = set()
                for _pb_df in _prop_books_pb.values():
                    if _pb_df.empty:
                        continue
                    _has_price = _pb_df["over_price"].notna() | _pb_df["under_price"].notna()
                    _prop_books.update(_pb_df.loc[_has_price, "book"].unique())

                every_book = sorted(set(
                    b for df in _markets_pb.values() if not df.empty for b in df["book"].unique()
                ) | _prop_books)

                # Combined fair probability is now computed PER BOOK,
                # not once book-independent as before Player Props became
                # line-agnostic. Spread/Total/Moneyline legs still use
                # exactly one representative line (or no line at all) for
                # every book, so their own fair probability (_leg_fair_prob,
                # unchanged) is genuinely identical regardless of book --
                # recomputing it per book changes nothing for them, and
                # this still treats every leg as statistically independent,
                # the same explicit, documented assumption as before (no
                # canonical parlay-level fair-probability methodology
                # exists elsewhere in this repo, and this does not attempt
                # to model correlation between legs). Prop legs are
                # different now: two books can each carry the SAME selected
                # Player+Market+Side at two DIFFERENT thresholds, and
                # P(player over 58.5) is a genuinely different probability
                # from P(player over 60.5) -- there is no single "the" fair
                # probability for a line-agnostic prop leg anymore. Each
                # book's own EV% must use ITS OWN line's already-computed
                # consensus fair probability (_prop_fair_prob_for_line, an
                # opportunistic lookup against the unmodified FVB pipeline
                # output in df_props) when that specific line happens to
                # have cleared the existing 2-book coverage gate, and must
                # honestly show "—" rather than reuse a mismatched
                # probability when it hasn't -- never interpolated,
                # averaged, or substituted from a different line. Still
                # reuses expected_value_pct (core/odds_math.py) unmodified
                # -- no new EV formula.
                results = []
                _prop_line_disclosure_rows = []
                for book in every_book:
                    combined_dec = 1.0
                    valid = True
                    _book_fair_prob = 1.0
                    _book_fair_prob_valid = True
                    _book_prop_lines = []  # (leg, this book's own line) pairs, for the transparency table below
                    for leg in st.session_state.pb_parlay_legs:
                        if leg.get("Type") == "prop":
                            _pb_df = _prop_books_pb.get(leg["Market"])
                            if _pb_df is None or _pb_df.empty:
                                valid = False; break
                            _target_key = normalize_player_key(leg.get("Player"))
                            _price_col = "over_price" if leg.get("Side") == "Over" else "under_price"
                            _s = _pb_df[
                                (_pb_df["book"] == book) &
                                (_pb_df["player_key"] == _target_key) &
                                ((_pb_df["home_team"] + " vs " + _pb_df["away_team"]) == leg["Game"]) &
                                _pb_df[_price_col].notna()
                            ]
                            if _s.empty:
                                valid = False; break
                            # A book posts one live line per player/market
                            # in the normal case; if more than one row is
                            # present (e.g. a captured line move), the
                            # first is used -- same "first row wins"
                            # convention already used for the game-market
                            # raw pivot above, not a new tie-break rule.
                            _prow = _s.iloc[0]
                            price = _prow[_price_col]
                            _book_line = _prow.get("line")
                            _book_prop_lines.append((leg, _book_line))
                            _fp = _prop_fair_prob_for_line(df_props, leg, _book_line)
                            if _fp is None:
                                _book_fair_prob_valid = False
                            else:
                                _book_fair_prob *= _fp
                        else:
                            src = _markets_pb.get(leg["Market"])
                            if src is None or src.empty:
                                valid = False; break
                            s = src[
                                (src["book"] == book) &
                                ((src["home_team"] + " vs " + src["away_team"]) == leg["Game"])
                            ]
                            # Exact-line fix: book+game alone isn't enough for
                            # Spread/Total -- different books can carry
                            # different thresholds for the same game (a book
                            # at -2.5 must never be treated as carrying a
                            # selected -3, same for a book at 47.5 vs a
                            # selected 48). _markets_pb's raw per-book pivot
                            # (build_books_df, unmodified) already carries the
                            # exact numeric "line" (Spread, home-signed) /
                            # "total" (Total) column needed to enforce this --
                            # no new data, just the equality check that was
                            # missing. Moneyline has no line concept and is
                            # untouched.
                            if leg["Market"] == "Spread" and not s.empty:
                                _home_team_name, _, _ = leg["Game"].partition(" vs ")
                                try:
                                    _expected_line = (
                                        float(leg["Line"]) if leg["Pick"] == _home_team_name
                                        else -float(leg["Line"])
                                    )
                                    s = s[s["line"] == _expected_line]
                                except (TypeError, ValueError):
                                    s = s.iloc[0:0]
                            elif leg["Market"] == "Total" and not s.empty:
                                try:
                                    s = s[s["total"] == float(leg["Line"])]
                                except (TypeError, ValueError):
                                    s = s.iloc[0:0]
                            if s.empty:
                                valid = False; break
                            row = s.iloc[0]
                            if leg["Market"] == "Moneyline":
                                price = row["home_price"] if leg["Pick"] == row["home_team"] else row["away_price"]
                            elif leg["Market"] == "Spread":
                                price = row["home_price"] if leg["Pick"] == row["home_team"] else row["away_price"]
                            else:
                                price = row["over_price"] if leg["Pick"].lower() == "over" else row["under_price"]
                            _fp = _leg_fair_prob(leg, df_all_combined)
                            if _fp is None:
                                _book_fair_prob_valid = False
                            else:
                                _book_fair_prob *= _fp
                        try:
                            combined_dec *= american_to_decimal(price)
                        except Exception:
                            valid = False; break
                    if valid and combined_dec > 1.0:
                        # Combined price computed exactly as before (American
                        # `pa`, decimal `combined_dec`) — Odds Format only
                        # changes how `pa` is displayed below, never this math.
                        pa = int((combined_dec - 1) * 100) if combined_dec >= 2 else int(-100 / (combined_dec - 1))
                        _profit = round(stake * (combined_dec - 1), 2)
                        _ev_str = (
                            fmt_ev(expected_value_pct(_book_fair_prob, pa))
                            if _book_fair_prob_valid else "—"
                        )
                        results.append({
                            "Sportsbook": book,
                            "Odds": _fmt_odds_in_format(pa, _odds_format),
                            "Payout ($)": f"${round(stake * combined_dec, 2):,.2f}",
                            "Profit ($)": f"${_profit:,.2f}",
                            "EV%": _ev_str,
                        })
                        for _leg, _line_val in _book_prop_lines:
                            if pd.notna(_line_val):
                                _prop_line_disclosure_rows.append({
                                    "Sportsbook": book,
                                    "Prop Leg": _prop_bet_label(_leg),
                                    "Actual Line": f"{float(_line_val):g}",
                                })

                if not results:
                    st.warning("No sportsbook has all selected legs available.")
                else:
                    st.markdown("### Parlay Comparison")
                    st.dataframe(
                        pd.DataFrame(results).sort_values("Payout ($)", ascending=False),
                        use_container_width=True, hide_index=True,
                    )
                    if _prop_line_disclosure_rows:
                        # Transparency requirement: different sportsbooks
                        # can now use different actual thresholds for the
                        # same selected Player Prop leg (that's the whole
                        # point of this design) -- this must never be
                        # implied to be the same wager. Kept out of the
                        # main comparison table (which stays concise) and
                        # surfaced here instead, same lightweight
                        # expander+table pattern already used elsewhere in
                        # this app for secondary detail.
                        with st.expander("View sportsbook-specific prop lines"):
                            st.caption(
                                "Player Prop legs use each sportsbook's own actual "
                                "posted line — thresholds can differ by book."
                            )
                            st.dataframe(
                                pd.DataFrame(_prop_line_disclosure_rows),
                                use_container_width=True, hide_index=True,
                            )

        st.divider()
        st.markdown("### Browse Games")
        st.caption("Scroll through every game and market. Click Add to build your parlay. "
                   "Only one pick per market, per game is allowed.")

        # Game Markets / Player Props is a browsing-mode toggle only --
        # it decides what each game card's single inner expander shows
        # (see below), not a second, separately-nested expander. Current
        # Parlay, the same-game disclosure, and Compare Parlay Odds above
        # are all mode-independent and already handle both leg types via
        # df_all_combined regardless of which mode is selected here.
        _pb_mode = st.segmented_control(
            "Mode", ["Game Markets", "Player Props"], default="Game Markets",
            key="pb_mode", label_visibility="collapsed",
        ) or "Game Markets"

        if df_all.empty:
            st.info(f"No games found for {caption_label}.")
            return

        _locked_markets = {
            (l["Game"], l["_commence"], l["Market"]): l
            for l in st.session_state.pb_parlay_legs
            if "_commence" in l
        }

        _games_ordered = (
            df_all.assign(_sort_dt=df_all["commence_time"].apply(parse_iso_dt_utc))
            .dropna(subset=["_sort_dt"])
            [["Game", "commence_time", "_sort_dt"]]
            .drop_duplicates(subset=["Game", "commence_time"])
            .sort_values("_sort_dt")
            [["Game", "commence_time"]]
            .to_records(index=False)
            .tolist()
        )

        _last_bucket = None
        for _game_label, _commence_iso in _games_ordered:
            _game_rows = df_all[
                (df_all["Game"] == _game_label) & (df_all["commence_time"] == _commence_iso)
            ].reset_index(drop=True)
            if _game_rows.empty:
                continue
            _dt = parse_iso_dt_utc(_commence_iso)
            if not _dt:
                continue
            _et = _dt.astimezone(EASTERN)
            _bucket = _et.replace(minute=0, second=0, microsecond=0)

            if _bucket != _last_bucket:
                st.markdown(f"##### {_bucket.strftime('%a %I:%M %p ET').lstrip('0').replace(' 0', ' ')}")
                _last_bucket = _bucket

            _teams = _game_label.split(" vs ")
            _ht = _teams[0] if len(_teams) == 2 else _game_label
            _at = _teams[1] if len(_teams) == 2 else ""
            _away_logo = _logo_url(_at)
            _home_logo = _logo_url(_ht)

            # Outer bordered matchup card + inner collapsible bets section,
            # mirroring tabs/matchup_center.py's card-within-card pattern
            # (st.container(border=True) as the outer card, an
            # st.expander nested inside that same container as the inner
            # collapsible section — Streamlit allows an expander inside a
            # bordered container, just not inside another expander).
            # st.expander labels are plain text only (no markdown/HTML),
            # so logos live in the outer card's header instead, above the
            # expander, both inside the same bordered container.
            with st.container(border=True):
                if _away_logo and _home_logo:
                    st.markdown(
                        f"<img src='{_away_logo}' width='20' style='vertical-align:middle;margin-right:4px'/>"
                        f"**{_at}** @ "
                        f"<img src='{_home_logo}' width='20' style='vertical-align:middle;margin:0 4px'/>"
                        f"**{_ht}**",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(f"**{_at} @ {_ht}**")

                # One inner expander per card, whichever mode is active --
                # never a second "Player Props" expander stacked inside
                # "Bets". The label and contents both swap on _pb_mode.
                with st.expander("Props" if _pb_mode == "Player Props" else "Bets", expanded=False):
                    if _pb_mode == "Player Props":
                        # Player -> Market -> Over/Under, driven by the raw
                        # (ungated) per-book pivot loaded above, not the
                        # consensus-priced df_props -- a market/side is
                        # offered here as soon as ANY book prices it, even
                        # if that book is the only one at its own specific
                        # line (see _load_parlay_builder_prop_books'
                        # docstring). The player is only ever deciding a
                        # direction now, not a threshold, so no
                        # representative-line selection happens here at all.
                        _game_prop_players = set()
                        for _pb_df in _prop_books_pb.values():
                            if _pb_df.empty:
                                continue
                            _g = _pb_df[(_pb_df["home_team"] + " vs " + _pb_df["away_team"]) == _game_label]
                            if not _g.empty:
                                _game_prop_players.update(_g["player_display"].dropna().unique())

                        if not _game_prop_players:
                            st.caption("No player props available for this game yet.")
                        else:
                            _picked_player = st.selectbox(
                                "Player", sorted(_game_prop_players),
                                key=f"pb_prop_player_{_game_label}_{_commence_iso}",
                                label_visibility="collapsed",
                            )
                            _target_player_key = normalize_player_key(_picked_player)

                            # Player is already chosen via the selectbox
                            # above -- the market header doesn't repeat it.
                            _any_market_rendered = False
                            for _prop_mkt_key, _prop_cfg in PROP_MARKETS.items():
                                _prop_mkt_label = _prop_cfg.market_label
                                _mkt_pb_df = _prop_books_pb.get(_prop_mkt_label, pd.DataFrame())
                                if _mkt_pb_df.empty:
                                    continue
                                _player_rows = _mkt_pb_df[
                                    ((_mkt_pb_df["home_team"] + " vs " + _mkt_pb_df["away_team"]) == _game_label)
                                    & (_mkt_pb_df["player_key"] == _target_player_key)
                                ]
                                if _player_rows.empty:
                                    continue
                                _has_over = _player_rows["over_price"].notna().any()
                                _has_under = _player_rows["under_price"].notna().any()
                                if not _has_over and not _has_under:
                                    continue
                                _any_market_rendered = True

                                # Informational only -- the mean of every
                                # available sportsbook's own line for this
                                # game/player/market (one observation per
                                # book, from the same raw per-book pivot
                                # driving eligibility above; a book that
                                # changed its line intraday still
                                # contributes its "first row wins" line,
                                # matching the convention already used
                                # elsewhere for this same pivot). Never
                                # stored on the leg, never used to gate
                                # sportsbook eligibility or to look up a
                                # fair probability -- Compare Parlay Odds
                                # continues to resolve each book's own
                                # actual line/price exactly as before.
                                _avg_line_vals = _player_rows["line"].dropna()
                                _avg_line = (
                                    sum(float(v) for v in _avg_line_vals) / len(_avg_line_vals)
                                    if len(_avg_line_vals) else None
                                )
                                if _avg_line is not None:
                                    _avg_unit = _PROP_AVG_UNIT.get(_prop_mkt_label, "")
                                    _avg_text = _fmt_avg_line(_avg_line) + (f" {_avg_unit}" if _avg_unit else "")
                                    _mkt_header = f"**{_prop_mkt_label}** · {_avg_text}"
                                else:
                                    _mkt_header = f"**{_prop_mkt_label}**"
                                st.markdown(_mkt_header)

                                # Only one side of one market can be
                                # selected at a time -- same "one pick per
                                # market, per game" rule Game Markets
                                # already enforces (Over/Under of the same
                                # prop are mutually exclusive real-world
                                # bets, exactly like Home/Away Moneyline).
                                _added_leg_for_mkt = next(
                                    (l for l in st.session_state.pb_parlay_legs
                                     if l.get("Type") == "prop" and l.get("_commence") == _commence_iso
                                     and l.get("Market") == _prop_mkt_label and l.get("Player") == _picked_player),
                                    None,
                                )
                                _added_side = _added_leg_for_mkt.get("Side") if _added_leg_for_mkt else None

                                _oc, _uc = st.columns(2)
                                for _col, _side_available, _side_name in [(_oc, _has_over, "Over"), (_uc, _has_under, "Under")]:
                                    if not _side_available:
                                        continue
                                    # No Line stored -- a Player Prop leg is
                                    # now identified by Game/Player/Market/
                                    # Side only; each sportsbook resolves
                                    # its own actual line at Compare time.
                                    _side_leg = {
                                        "Type": "prop", "Market": _prop_mkt_label, "Game": _game_label,
                                        "Pick": f"{_picked_player} {_side_name}", "Player": _picked_player,
                                        "Side": _side_name, "_commence": _commence_iso,
                                    }
                                    _btn_key = f"pb_add_prop_{_game_label}_{_commence_iso}_{_prop_mkt_label}_{_picked_player}_{_side_name}"
                                    if _added_side == _side_name:
                                        if _col.button(f"✓ {_side_name}", key=_btn_key,
                                                        use_container_width=True, type="primary"):
                                            st.session_state.pb_parlay_legs = [
                                                l for l in st.session_state.pb_parlay_legs
                                                if not (
                                                    l.get("Type") == "prop" and l.get("_commence") == _commence_iso
                                                    and l.get("Market") == _prop_mkt_label
                                                    and l.get("Player") == _picked_player
                                                    and l.get("Side") == _side_name
                                                )
                                            ]
                                            _fragment_rerun()
                                    elif _added_side is not None:
                                        _col.button(_side_name, key=_btn_key, disabled=True, use_container_width=True)
                                    else:
                                        if _col.button(_side_name, key=_btn_key, use_container_width=True):
                                            st.session_state.pb_parlay_legs.append(_side_leg)
                                            _fragment_rerun()
                            if not _any_market_rendered:
                                st.caption("No player props available for this player yet.")
                    else:
                        for _mkt_name in ["Moneyline", "Spread", "Total"]:
                            _mkt_rows = _game_rows[_game_rows["Market"] == _mkt_name]
                            if _mkt_rows.empty:
                                continue

                            # Spread/Total: narrow every distinct exact line down to
                            # one representative line, same rule and same helper as
                            # Player Props -- Moneyline has no line concept and is
                            # left completely untouched. Spread groups by absolute
                            # value (see _select_representative_line's docstring --
                            # a single threshold splits into a "-3" home row and a
                            # "+3" away row, which must be treated as one candidate,
                            # not two). Total groups by its own Line string directly,
                            # same as Player Props, since Over/Under already share
                            # one identical Line string per threshold.
                            if _mkt_name == "Spread":
                                _mkt_rows = _select_representative_line(
                                    _mkt_rows, tiebreak="abs",
                                    group_key_fn=lambda l: abs(float(l)),
                                )
                            elif _mkt_name == "Total":
                                _mkt_rows = _select_representative_line(_mkt_rows, tiebreak="lower")
                            if _mkt_rows.empty:
                                continue

                            _lock_key = (_game_label, _commence_iso, _mkt_name)
                            _locked_leg = _locked_markets.get(_lock_key)

                            st.caption(_mkt_name)
                            # Each row already fully identifies one bet (Pick + its
                            # own Line + its own Best Odds) — no need for a separate
                            # per-line-value grouping pass, which just meant more
                            # repeated captions for the same information. Row text
                            # (e.g. "Lions -2 (-109)") matches the same concise
                            # "side (odds)" format used everywhere in this card.
                            # Spread/Total now show no odds at all -- the user is
                            # only deciding a side on the representative line here;
                            # pricing returns at Compare Parlay Odds, same as
                            # Player Props.
                            for _, _r in _mkt_rows.iterrows():
                                _pick_label = _r.get("Pick", "—")
                                _row_line = _r.get("Line")
                                _leg_line = "ML" if _mkt_name == "Moneyline" else _row_line
                                _leg = {
                                    "Type": "game", "Market": _mkt_name, "Game": _game_label,
                                    "Pick": _pick_label, "Line": _leg_line, "_commence": _commence_iso,
                                }
                                _is_this_leg_added = (
                                    _locked_leg is not None
                                    and _locked_leg["Pick"] == _pick_label
                                    and _locked_leg["Line"] == _leg_line
                                )
                                if _mkt_name == "Moneyline":
                                    _best_odds = _r.get("Best Odds")
                                    _odds_str = (
                                        _fmt_odds_in_format(int(_best_odds), _odds_format)
                                        if pd.notna(_best_odds) else "—"
                                    )
                                    _row_core = _short_team(_pick_label)
                                    _row_text = f"{_row_core} ({_odds_str})"
                                elif _mkt_name == "Total":
                                    _row_core = f"{_pick_label} {_row_line}" if pd.notna(_row_line) else _pick_label
                                    _row_text = _row_core
                                else:
                                    _row_core = (
                                        f"{_short_team(_pick_label)} {_row_line}"
                                        if pd.notna(_row_line) else _short_team(_pick_label)
                                    )
                                    _row_text = _row_core
                                _rc1, _rc2 = st.columns([5, 2])
                                _rc1.write(_row_text)
                                _btn_key = f"pb_add_{_game_label}_{_commence_iso}_{_mkt_name}_{_pick_label}_{_row_line}"
                                if _is_this_leg_added:
                                    if _rc2.button("Remove", key=_btn_key, use_container_width=True):
                                        st.session_state.pb_parlay_legs = [
                                            l for l in st.session_state.pb_parlay_legs
                                            if not (
                                                l.get("_commence") == _commence_iso
                                                and l["Market"] == _mkt_name
                                                and l["Pick"] == _pick_label
                                                and l["Line"] == _leg_line
                                            )
                                        ]
                                        _fragment_rerun()
                                elif _locked_leg is not None:
                                    _rc2.button("Locked", key=_btn_key, disabled=True, use_container_width=True)
                                else:
                                    if _rc2.button("Add", key=_btn_key, use_container_width=True):
                                        st.session_state.pb_parlay_legs.append(_leg)
                                        _fragment_rerun()

    _parlay_builder_body()
