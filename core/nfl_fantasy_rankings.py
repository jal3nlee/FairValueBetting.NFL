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
# within a position: every ranked player at a position is scored from
# the exact same set of required markets, so a market's absence for one
# player can never silently advantage or disadvantage another player in
# the same table (no optional/omitted components in the ranking score).
#   QB      : Passing Yards, Passing TDs
#   RB      : Rushing Yards, Receptions, Receiving Yards, Anytime TD
#   WR / TE : Receptions, Receiving Yards, Anytime TD
# Interceptions and QB rushing/Anytime-TD are ingested (see
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
# this dict IS the "consistent inputs" contract: a position's ranked
# table can only ever be built from exactly this market set.
_REQUIRED_MARKETS = {
    "QB": ["player_pass_yds", "player_pass_tds"],
    "RB": ["player_rush_yds", "player_receptions", "player_reception_yds", "player_anytime_td"],
    "WR": ["player_receptions", "player_reception_yds", "player_anytime_td"],
    "TE": ["player_receptions", "player_reception_yds", "player_anytime_td"],
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

    Returns:
      {
        "QB": DataFrame[Player, Team, Pos, FVB Fantasy Pts, Passing Yards, Passing TDs, _detail],
        "RB": DataFrame[...], "WR": DataFrame[...], "TE": DataFrame[...],
        "excluded": {"QB": n, "RB": n, "WR": n, "TE": n},   # players with a qualifying prop but missing >=1 required market or no roster match
        "detail": {player_key: {market_label: {"value"/"prob", "num_books": n}, ...}},
      }
    Never estimates a missing required market — a player missing any
    required market for his position is excluded from that position's
    table entirely, counted in "excluded", not scored with a zero.
    """
    empty = {p: pd.DataFrame(columns=["Player", "Team", "Pos", "FVB Fantasy Pts"]) for p in RANKING_POSITIONS}
    if not event_ids:
        return {**empty, "excluded": {p: 0 for p in RANKING_POSITIONS}, "detail": {}}

    def _load(market_key: str) -> pd.DataFrame:
        raw = fetch_prop_market_lines(supabase, list(event_ids), market_key)
        return filter_by_window(raw, window_start, window_end)

    line_markets = {"player_pass_yds", "player_rush_yds", "player_reception_yds", "player_receptions"}
    market_data: dict = {}
    for mkt in {"player_pass_yds", "player_pass_tds", "player_rush_yds",
                "player_reception_yds", "player_receptions"}:
        market_data[mkt] = weighted_market_lines(_load(mkt), mkt)
    market_data["player_anytime_td"] = anytime_td_expectations(_load("player_anytime_td"))

    lookup = {
        mkt: {row["player_key"]: row for row in df.to_dict("records")}
        for mkt, df in market_data.items()
    }

    roster_map = _roster_position_map()

    detail: dict = {}
    excluded = {p: 0 for p in RANKING_POSITIONS}
    rows_by_pos = {p: [] for p in RANKING_POSITIONS}

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
            continue

        position = roster_info["position"]
        required = _REQUIRED_MARKETS.get(position)
        if not required:
            continue

        rows = {mkt: lookup.get(mkt, {}).get(player_key) for mkt in required}
        if any(r is None for r in rows.values()):
            excluded[position] += 1
            continue

        player_detail = {}
        for mkt, r in rows.items():
            label = PROP_MARKETS[mkt].market_label
            if mkt == "player_anytime_td":
                player_detail[label] = {"prob": r["td_prob"], "lambda": r["td_lambda"],
                                         "fantasy_pts": r["td_fantasy_pts"], "num_books": r["num_books"]}
            else:
                player_detail[label] = {"value": r["weighted_line"], "num_books": r["num_books"]}

        if position == "QB":
            pts = _qb_points(rows["player_pass_yds"]["weighted_line"], rows["player_pass_tds"]["weighted_line"])
        elif position == "RB":
            pts = _rb_points(
                rows["player_rush_yds"]["weighted_line"], rows["player_reception_yds"]["weighted_line"],
                rows["player_receptions"]["weighted_line"], rows["player_anytime_td"]["td_fantasy_pts"], scoring,
            )
        else:  # WR / TE
            pts = _wrte_points(
                rows["player_reception_yds"]["weighted_line"], rows["player_receptions"]["weighted_line"],
                rows["player_anytime_td"]["td_fantasy_pts"], scoring,
            )

        display_name = roster_info["display_name"] or next(iter(rows.values()))["player_display"]
        row_out = {
            "Player": display_name, "Team": roster_info["team"] or "", "Pos": position,
            "FVB Fantasy Pts": round(pts, 2), "_player_key": player_key,
        }
        for mkt, r in rows.items():
            label = PROP_MARKETS[mkt].market_label
            if mkt == "player_anytime_td":
                row_out[f"{label} (λ pts)"] = round(r["td_fantasy_pts"], 2)
            else:
                row_out[label] = round(r["weighted_line"], 1)
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
    return result
