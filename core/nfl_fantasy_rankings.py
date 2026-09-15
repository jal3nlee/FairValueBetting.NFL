# core/nfl_fantasy_rankings.py
# Phase 1 Fantasy Rankings — a translation of sportsbook markets into
# fantasy points, NOT an independent player-performance projection. Every
# number here is either a sportsbook-weighted consensus of a stored,
# ingested market (core/nfl_prop_pipeline.py::build_prop_books_df,
# core/pipeline.py's BOOK_WEIGHTS/_book_weight/_consensus_engine — all
# imported and used UNMODIFIED) or a mechanical, declared conversion of
# one (the Anytime TD Poisson step below). nflreadpy rosters are used
# ONLY to attach a current position/team to a prop row for display and
# formula selection — never to adjust, blend, or estimate a stat value.
#
# Phase 1 scoring inputs are deliberately narrow and APPLES-TO-APPLES
# within a position for every REQUIRED market: a player missing any
# required market for his position is excluded outright, never zero-filled.
#   QB      : Passing Yards, Passing TDs                          (both required)
#   RB      : Rushing Yards, Receptions, Receiving Yards           (required)
#             + Anytime TD                                        (optional/additive)
#   WR / TE : Receptions, Receiving Yards                          (required)
#             + Anytime TD                                        (optional/additive)
# Anytime TD coverage from a single unified sportsbook feed turned out to
# be thin enough (2+ books) that requiring it excluded most otherwise-
# fully-covered RB/WR/TE players -- so it is now OPTIONAL/ADDITIVE per an
# explicit product decision: when a valid 2+ book Anytime TD consensus
# exists for a player, its Poisson-derived fantasy-point contribution
# (unchanged math) is added on top of the required-market score; when it
# doesn't, the player is NOT excluded and NO TD contribution is added --
# never a fabricated/estimated/zero-implied probability. This is still
# apples-to-apples WITHIN what's shown: the detail/UI layer marks exactly
# which players had TD coverage and which didn't, rather than blending an
# invisible zero into two different players' scores.
# Interceptions and QB rushing/Anytime-TD-for-QB are ingested (see
# core/nfl_prop_market_config.py::PROP_MARKETS) but deliberately NOT
# consumed by this module's Phase 1 score — infrastructure for a future
# phase once a consistent-coverage policy for them is established.
import math

import pandas as pd

from core.pipeline import _book_weight, _consensus_engine
from core.nfl_prop_market_config import PROP_MARKETS, MIN_BOOKS_FOR_PROP_CONSENSUS, normalize_player_key
from core.nfl_prop_pipeline import build_prop_books_df
from core.nfl_prop_data_sources import fetch_prop_market_lines
from core.data_sources import filter_by_window
from core.nflverse_data import _current_roster_rows, get_current_season
from core.lineup_data import (
    _REC_PTS_BY_SCORING, _PTS_PER_PASS_YARD, _PTS_PER_PASS_TD,
    _PTS_PER_RUSH_YARD, _PTS_PER_REC_YARD, _PTS_PER_TD,
)

SCORING_OPTIONS = ["PPR", "Half PPR", "Standard"]
RANKING_POSITIONS = ["QB", "RB", "WR", "TE"]

# Required stored prop market(s) per position, keyed to PROP_MARKETS —
# this dict IS the eligibility contract: a player missing ANY of these
# for his position is excluded outright, never zero-filled.
_REQUIRED_MARKETS = {
    "QB": ["player_pass_yds", "player_pass_tds"],
    "RB": ["player_rush_yds", "player_receptions", "player_reception_yds"],
    "WR": ["player_receptions", "player_reception_yds"],
    "TE": ["player_receptions", "player_reception_yds"],
}

# Optional/additive market(s) per position: contribute their fantasy-point
# component when a valid 2+ book consensus exists, otherwise contribute
# nothing and never exclude the player. Never zero-filled or estimated.
_OPTIONAL_MARKETS = {
    "QB": [],
    "RB": ["player_anytime_td"],
    "WR": ["player_anytime_td"],
    "TE": ["player_anytime_td"],
}


# =======================
# WEIGHTED LINE VALUE (continuous/counting props)
# =======================
def weighted_market_lines(raw_lines: pd.DataFrame, market_key: str) -> pd.DataFrame:
    """
    One BOOK_WEIGHTS-weighted consensus LINE VALUE per player for a
    two-sided numeric-line market — e.g. several books post 64.5 / 67.5 /
    65.5 receiving yards, each weighted by its existing FVB sportsbook
    weight, producing one weighted line. This is deliberately NOT a
    probability/devig calculation (no Over/Under prices are touched) and
    introduces no distribution assumption — it is a weighted average of
    the sportsbooks' own posted numbers, using the exact same
    core/pipeline.py::BOOK_WEIGHTS / _book_weight already used everywhere
    else in this app.

    Duplicate-sportsbook protection: reuses build_prop_books_df's
    existing per-book pivot unmodified. That function already guarantees
    each book contributes at most one row per (event_id, player_key,
    line) — a book's Over row and Under row are merged into one row via
    groupby(...).agg(max) before this function ever sees the data — and
    the raw_lines this is called with are already scoped to the single
    LATEST snapshot per event+market (core/nfl_prop_data_sources.py::
    fetch_prop_market_lines -> get_latest_prop_snapshot_meta orders by
    pulled_at desc and takes exactly one snapshot_id), so no stale/older
    pull can contribute a second, duplicate row for the same book. A book
    therefore contributes to this weighted average exactly once.

    Weight renormalization: Σ(weight_i) is computed only over the books
    that actually appear for that player+market group — a missing book
    is simply absent from both the numerator and denominator, which is
    already the correct renormalization (identical in spirit to how
    _consensus_engine renormalizes fair probabilities today).

    Returns columns: player_key, player_display, weighted_line, num_books.
    Groups with fewer than MIN_BOOKS_FOR_PROP_CONSENSUS contributing
    books are excluded entirely (not defaulted to a single-book value).
    """
    cfg = PROP_MARKETS[market_key]
    books_df = build_prop_books_df(raw_lines, cfg)
    if books_df.empty:
        return pd.DataFrame(columns=["player_key", "player_display", "weighted_line", "num_books"])

    books_df = books_df.dropna(subset=["line"]).copy()
    if books_df.empty:
        return pd.DataFrame(columns=["player_key", "player_display", "weighted_line", "num_books"])

    books_df["_weight"] = books_df["book"].apply(_book_weight)

    def _agg(g: pd.DataFrame) -> pd.Series:
        w_sum = g["_weight"].sum()
        weighted_line = (g["line"] * g["_weight"]).sum() / w_sum if w_sum > 0 else None
        return pd.Series({
            "player_display": g["player_display"].iloc[0],
            "weighted_line": weighted_line,
            "num_books": int(g["book"].nunique()),
        })

    out = books_df.groupby("player_key").apply(_agg).reset_index()
    out = out[out["num_books"] >= MIN_BOOKS_FOR_PROP_CONSENSUS].reset_index(drop=True)
    return out


# =======================
# ANYTIME TD -> POISSON FANTASY-POINT CONVERSION
# =======================
def anytime_td_expectations(raw_lines: pd.DataFrame) -> pd.DataFrame:
    """
    Mechanical market translation, not a player-specific model:
      1. per-book Yes/No devig                (core/pipeline.py::_implied_prob_no_vig, unmodified)
      2. existing BOOK_WEIGHTS -> fair consensus P(TD>=1)  (core/pipeline.py::_consensus_engine, unmodified)
      3. lambda = -ln(1 - P)
      4. expected_td_points = lambda * 6.0     (core/lineup_data.py::_PTS_PER_TD, reused unchanged)

    Returns columns: player_key, player_display, td_prob, td_lambda,
    td_fantasy_pts, num_books. Groups below MIN_BOOKS_FOR_PROP_CONSENSUS
    are excluded, matching every other market's coverage gate.
    """
    cfg = PROP_MARKETS["player_anytime_td"]
    books_df = build_prop_books_df(raw_lines, cfg)
    if books_df.empty:
        return pd.DataFrame(columns=["player_key", "player_display", "td_prob", "td_lambda",
                                      "td_fantasy_pts", "num_books"])

    cons = _consensus_engine(
        df=books_df, group_keys=["player_key"], side_a_price=cfg.price_a_col, side_b_price=cfg.price_b_col,
        out_a=cfg.fair_a_col, out_b=cfg.fair_b_col, label=cfg.name,
    )
    if cons.empty:
        return pd.DataFrame(columns=["player_key", "player_display", "td_prob", "td_lambda",
                                      "td_fantasy_pts", "num_books"])

    cons = cons[cons["num_books"] >= MIN_BOOKS_FOR_PROP_CONSENSUS].reset_index(drop=True)
    if cons.empty:
        return pd.DataFrame(columns=["player_key", "player_display", "td_prob", "td_lambda",
                                      "td_fantasy_pts", "num_books"])

    def _lambda(p):
        if p is None or pd.isna(p) or p < 0 or p >= 1:
            return None
        return -math.log(1 - p)

    cons["td_prob"] = cons[cfg.fair_a_col]
    cons["td_lambda"] = cons["td_prob"].apply(_lambda)
    cons["td_fantasy_pts"] = cons["td_lambda"].apply(lambda l: (l * _PTS_PER_TD) if l is not None else None)

    display = books_df[["player_key", "player_display"]].drop_duplicates(subset=["player_key"])
    cons = cons.merge(display, on="player_key", how="left")
    return cons[["player_key", "player_display", "td_prob", "td_lambda", "td_fantasy_pts", "num_books"]]


# =======================
# POSITION / TEAM ATTACHMENT (nflreadpy rosters — identity only, never a stat)
# =======================
def _roster_position_map() -> dict:
    """
    {player_key: {"position": ..., "team": ..., "display_name": ...}} for
    the current nflreadpy roster, keyed by the SAME normalize_player_key
    used for prop rows so the two identity spaces line up exactly. This
    is identity/context only (position, team) — no stat, projection, or
    historical performance value is read from rosters anywhere in this
    module.
    """
    season = get_current_season()
    if season is None:
        return {}
    try:
        roster_rows = _current_roster_rows(season)
    except Exception:
        return {}
    if not roster_rows:
        return {}

    out: dict = {}
    for r in roster_rows:
        position = r.get("position")
        if position not in RANKING_POSITIONS:
            continue
        name = r.get("full_name") or r.get("player_name")
        key = normalize_player_key(name)
        if not key:
            continue
        out[key] = {"position": position, "team": r.get("team"), "display_name": name}
    return out


# =======================
# PHASE 1 FANTASY-POINT FORMULAS (no optional components — see module docstring)
# =======================
def _qb_points(pass_yds: float, pass_tds: float) -> float:
    return pass_yds * _PTS_PER_PASS_YARD + pass_tds * _PTS_PER_PASS_TD


def _rb_points(rush_yds: float, rec_yds: float, receptions: float, td_pts: float, scoring: str) -> float:
    rec_pts = _REC_PTS_BY_SCORING.get(scoring, 0.0)
    return rush_yds * _PTS_PER_RUSH_YARD + rec_yds * _PTS_PER_REC_YARD + receptions * rec_pts + td_pts


def _wrte_points(rec_yds: float, receptions: float, td_pts: float, scoring: str) -> float:
    rec_pts = _REC_PTS_BY_SCORING.get(scoring, 0.0)
    return rec_yds * _PTS_PER_REC_YARD + receptions * rec_pts + td_pts


# =======================
# ASSEMBLY
# =======================
def build_fantasy_rankings(supabase, event_ids: list, window_start, window_end, scoring: str) -> dict:
    """
    Loads the required stored prop markets for the given events/window,
    computes weighted lines + Anytime TD expectations, and assembles one
    ranked table per position under the strict "same required markets for
    every ranked player" rule (see module docstring / _REQUIRED_MARKETS).
    Anytime TD (see _OPTIONAL_MARKETS) is additive when available and
    simply omitted -- never excluded on, never zero-filled -- when not.

    Returns:
      {
        "QB": DataFrame[Player, Team, Pos, FVB Fantasy Pts, Passing Yards, Passing TDs],
        "RB"/"WR"/"TE": DataFrame[..., "Anytime TD (λ pts)" (NaN when unavailable), "_td_available"],
        "excluded": {"QB": n, "RB": n, "WR": n, "TE": n},   # players with a qualifying prop but missing >=1 REQUIRED market or no roster match (never for missing Anytime TD alone)
        "detail": {player_key: {market_label: {"value"/"prob", "num_books": n} or {"unavailable": True}, ...}},
        "considered": {"QB": n, ...},        # diagnostics: players with >=1 required-market prop for that position
        "excluded_detail": {"QB": [...], ...},  # diagnostics: {player_key, display_name, team, position, missing:[labels]}
        "market_coverage": {market_key: n_players_passing_2plus_book_gate, ...},  # diagnostics, includes player_anytime_td even though it's optional
        "anytime_td_diag": {"raw_rows": n, "sides_present": [...], "players_2plus_books": n},  # diagnostics
      }
    The "diagnostics"-labeled keys are read-only bookkeeping surfaced for
    tabs/fantasy_rankings.py's Coverage Diagnostics expander -- they do not
    feed the scoring/eligibility logic above, which is unchanged.
    Never estimates a missing required market — a player missing any
    required market for his position is excluded from that position's
    table entirely, counted in "excluded", not scored with a zero.
    """
    empty = {p: pd.DataFrame(columns=["Player", "Team", "Pos", "FVB Fantasy Pts"]) for p in RANKING_POSITIONS}
    if not event_ids:
        return {
            **empty, "excluded": {p: 0 for p in RANKING_POSITIONS}, "detail": {},
            "considered": {p: 0 for p in RANKING_POSITIONS}, "excluded_detail": {p: [] for p in RANKING_POSITIONS},
            "market_coverage": {}, "anytime_td_diag": {"raw_rows": 0, "sides_present": [], "players_2plus_books": 0},
        }

    def _load(market_key: str) -> pd.DataFrame:
        raw = fetch_prop_market_lines(supabase, list(event_ids), market_key)
        return filter_by_window(raw, window_start, window_end)

    market_data: dict = {}
    for mkt in {"player_pass_yds", "player_pass_tds", "player_rush_yds",
                "player_reception_yds", "player_receptions"}:
        market_data[mkt] = weighted_market_lines(_load(mkt), mkt)
    td_raw = _load("player_anytime_td")
    market_data["player_anytime_td"] = anytime_td_expectations(td_raw)

    anytime_td_diag = {
        "raw_rows": 0 if td_raw is None or td_raw.empty else len(td_raw),
        "sides_present": [] if td_raw is None or td_raw.empty else sorted(td_raw["side"].dropna().unique().tolist()),
        "players_2plus_books": 0 if market_data["player_anytime_td"].empty else len(market_data["player_anytime_td"]),
    }
    market_coverage = {mkt: (0 if df.empty else len(df)) for mkt, df in market_data.items()}

    lookup = {
        mkt: {row["player_key"]: row for row in df.to_dict("records")}
        for mkt, df in market_data.items()
    }

    roster_map = _roster_position_map()

    detail: dict = {}
    excluded = {p: 0 for p in RANKING_POSITIONS}
    excluded_detail: dict = {p: [] for p in RANKING_POSITIONS}
    considered: dict = {p: 0 for p in RANKING_POSITIONS}
    rows_by_pos = {p: [] for p in RANKING_POSITIONS}

    for pos in RANKING_POSITIONS:
        considered[pos] = len(set().union(*(set(lookup.get(mkt, {}).keys()) for mkt in _REQUIRED_MARKETS[pos])))

    # Universe of candidate player_keys: anyone appearing in ANY of the
    # required markets for ANY position, so we never miss a player who
    # has props but happens to be absent from the roster feed (counted
    # as excluded, not silently dropped).
    all_prop_keys = set()
    for mkt in _REQUIRED_MARKETS["QB"] + _REQUIRED_MARKETS["RB"] + _REQUIRED_MARKETS["WR"]:
        all_prop_keys |= set(lookup.get(mkt, {}).keys())

    for player_key in sorted(all_prop_keys):
        roster_info = roster_map.get(player_key)
        if roster_info is None:
            # No confident position/team identity -> cannot place this
            # player in any positional ranking; exclude everywhere they
            # have a qualifying market rather than guessing a position.
            for pos in RANKING_POSITIONS:
                if player_key in lookup.get(_REQUIRED_MARKETS[pos][0], {}):
                    excluded[pos] += 1
                    any_row = next((lookup[m][player_key] for m in _REQUIRED_MARKETS[pos]
                                     if player_key in lookup.get(m, {})), None)
                    excluded_detail[pos].append({
                        "player_key": player_key,
                        "display_name": (any_row or {}).get("player_display", player_key),
                        "team": None, "position": "Unknown (no roster match)",
                        "missing": ["Roster/position match"],
                    })
            continue

        position = roster_info["position"]
        required = _REQUIRED_MARKETS.get(position)
        if not required:
            continue

        rows = {mkt: lookup.get(mkt, {}).get(player_key) for mkt in required}
        if any(r is None for r in rows.values()):
            excluded[position] += 1
            missing_labels = [PROP_MARKETS[m].market_label for m in required if rows[m] is None]
            excluded_detail[position].append({
                "player_key": player_key, "display_name": roster_info["display_name"],
                "team": roster_info["team"], "position": position, "missing": missing_labels,
            })
            continue

        # Optional markets: looked up the same way, but a missing one
        # never excludes the player and never contributes a zero-implied
        # value -- td_row stays None, and the None just isn't added below.
        optional_mkts = _OPTIONAL_MARKETS.get(position, [])
        optional_rows = {mkt: lookup.get(mkt, {}).get(player_key) for mkt in optional_mkts}
        td_row = optional_rows.get("player_anytime_td")
        td_pts = td_row["td_fantasy_pts"] if td_row is not None else 0.0

        player_detail = {}
        for mkt, r in rows.items():
            player_detail[PROP_MARKETS[mkt].market_label] = {"value": r["weighted_line"], "num_books": r["num_books"]}
        for mkt, r in optional_rows.items():
            label = PROP_MARKETS[mkt].market_label
            if r is None:
                player_detail[label] = {"unavailable": True}
            elif mkt == "player_anytime_td":
                player_detail[label] = {"prob": r["td_prob"], "lambda": r["td_lambda"],
                                         "fantasy_pts": r["td_fantasy_pts"], "num_books": r["num_books"]}
            else:
                player_detail[label] = {"value": r["weighted_line"], "num_books": r["num_books"]}

        if position == "QB":
            pts = _qb_points(rows["player_pass_yds"]["weighted_line"], rows["player_pass_tds"]["weighted_line"])
        elif position == "RB":
            pts = _rb_points(
                rows["player_rush_yds"]["weighted_line"], rows["player_reception_yds"]["weighted_line"],
                rows["player_receptions"]["weighted_line"], td_pts, scoring,
            )
        else:  # WR / TE
            pts = _wrte_points(
                rows["player_reception_yds"]["weighted_line"], rows["player_receptions"]["weighted_line"],
                td_pts, scoring,
            )

        display_name = roster_info["display_name"] or next(iter(rows.values()))["player_display"]
        row_out = {
            "Player": display_name, "Team": roster_info["team"] or "", "Pos": position,
            "FVB Fantasy Pts": round(pts, 2), "_player_key": player_key,
            "_td_available": td_row is not None,
        }
        for mkt, r in rows.items():
            row_out[PROP_MARKETS[mkt].market_label] = round(r["weighted_line"], 1)
        if "player_anytime_td" in optional_mkts:
            # NaN (not a string) when unavailable -- keeps this a uniform
            # numeric column for the DataFrame; the UI layer formats NaN
            # as "--" for display rather than implying a zero TD.
            row_out["Anytime TD (λ pts)"] = round(td_row["td_fantasy_pts"], 2) if td_row is not None else float("nan")
        rows_by_pos[position].append(row_out)
        detail[player_key] = player_detail

    result: dict = {}
    for pos in RANKING_POSITIONS:
        df = pd.DataFrame(rows_by_pos[pos])
        if not df.empty:
            df = df.sort_values("FVB Fantasy Pts", ascending=False).reset_index(drop=True)
            df.insert(0, "Rank", range(1, len(df) + 1))
        result[pos] = df
    result["excluded"] = excluded
    result["detail"] = detail
    result["considered"] = considered
    result["excluded_detail"] = excluded_detail
    result["market_coverage"] = market_coverage
    result["anytime_td_diag"] = anytime_td_diag
    return result
