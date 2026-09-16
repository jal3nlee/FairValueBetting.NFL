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
#
# Anytime TD probability sourcing (see anytime_td_expectations below):
# live provider investigation confirmed player_anytime_td is currently
# returned Yes-only by every tested bookmaker, and no adequate two-sided
# paired market exists through this provider. A book with true two-sided
# Yes/No pricing still uses the original, unmodified two-sided devig path
# (_consensus_engine). A book with only a Yes price is converted via a
# bookmaker-specific, market-derived margin estimate (from that SAME
# book's other currently-posted two-sided props) using a multiplicative
# devig adjustment — never a fixed/hand-set vig percentage, never
# historical rates, never player-specific. Both kinds of per-book fair
# probabilities are combined into one BOOK_WEIGHTS-weighted consensus.
import math

import pandas as pd

from core.pipeline import _book_weight, _consensus_engine, _implied_prob_no_vig
from core.odds_math import american_to_implied_prob
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

# ── Single-sided Anytime TD methodology (approved design, see
# core/nfl_fantasy_rankings.py's anytime_td_expectations docstring) ─────
# Provider investigation (live, this week) confirmed player_anytime_td is
# currently returned Yes-only by every tested US/US2 bookmaker, and that
# no adequate two-sided paired market (player_tds, player_rush_reception_tds)
# exists through this provider. A sportsbook's OWN currently-posted
# two-sided companion prop markets (Passing Yards, Passing TDs, Rushing
# Yards, Receiving Yards, Receptions) are used to estimate THAT
# sportsbook's current margin, market-derived, never a fixed/hand-set
# constant. A book needs at least this many qualifying (book, market)
# observations before its OWN margin estimate is trusted.
MIN_MARGIN_SAMPLES = 2
# The cross-book fallback (average margin across OTHER books that DID
# produce their own book-specific estimate) is only used when at least
# this many other book-specific estimates exist this week -- otherwise
# there is no defensible fallback and the book is omitted from the
# Anytime TD consensus entirely, per the approved hierarchy.
FALLBACK_MIN_BOOKS = 2
_COMPANION_MARKETS = ["player_pass_yds", "player_pass_tds", "player_rush_yds",
                      "player_reception_yds", "player_receptions"]


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
# BOOK-SPECIFIC MARGIN ESTIMATION (single-sided Anytime TD support)
# =======================
def _book_market_margins(raw_lines: pd.DataFrame, market_key: str) -> pd.DataFrame:
    """
    Per-book raw overround for ONE two-sided companion prop market: for
    every (book, player_key, line) group where a book posted BOTH sides,
    margin = implied_over + implied_under - 1 -- exactly the same `total`
    quantity core/pipeline.py::_consensus_engine already computes
    internally (as p_a+p_b) before normalizing away, just surfaced here
    instead of discarded. Uses build_prop_books_df and
    american_to_implied_prob UNMODIFIED -- introduces no new pricing
    math, only exposes an existing intermediate quantity.

    Returns columns: book, margin (one row per qualifying book/player/line
    observation -- aggregated across observations by the caller).
    """
    cfg = PROP_MARKETS[market_key]
    books_df = build_prop_books_df(raw_lines, cfg)
    if books_df.empty:
        return pd.DataFrame(columns=["book", "margin"])
    books_df = books_df.dropna(subset=[cfg.price_a_col, cfg.price_b_col]).copy()
    if books_df.empty:
        return pd.DataFrame(columns=["book", "margin"])
    books_df["_imp_a"] = books_df[cfg.price_a_col].apply(american_to_implied_prob)
    books_df["_imp_b"] = books_df[cfg.price_b_col].apply(american_to_implied_prob)
    books_df = books_df.dropna(subset=["_imp_a", "_imp_b"])
    if books_df.empty:
        return pd.DataFrame(columns=["book", "margin"])
    books_df["margin"] = books_df["_imp_a"] + books_df["_imp_b"] - 1
    return books_df[["book", "margin"]].reset_index(drop=True)


def _derive_book_margins(companion_raw_lines: dict) -> dict:
    """
    companion_raw_lines: {market_key: raw_lines_df} for the 5 companion
    two-sided prop markets (_COMPANION_MARKETS). Returns
    {book: {"margin": float, "n_samples": int}} -- a book's OWN average
    overround across however many (book, market) observations it
    qualified for this event/week. A book with fewer than
    MIN_MARGIN_SAMPLES qualifying observations is excluded here entirely
    (not given a margin) -- the caller decides whether to use the
    cross-book fallback or omit that book.

    Individual observed margins are clipped to >=0 before averaging: a
    single negative-margin observation is a data artifact (e.g. a stale
    cross-book arbitrage snapshot), not real sportsbook behavior, and
    letting it pull the average below zero would INFLATE the resulting
    fair probability above the book's own raw price -- never desirable
    for a devig-style adjustment, which should only ever remove margin,
    never add to it.
    """
    all_margins = []
    for market_key in _COMPANION_MARKETS:
        raw = companion_raw_lines.get(market_key)
        if raw is None or raw.empty:
            continue
        m = _book_market_margins(raw, market_key)
        if not m.empty:
            all_margins.append(m)
    if not all_margins:
        return {}
    combined = pd.concat(all_margins, ignore_index=True)
    out = {}
    for book, grp in combined.groupby("book"):
        vals = grp["margin"].clip(lower=0.0)
        out[book] = {"margin": float(vals.mean()), "n_samples": int(len(grp))}
    return out


def _fallback_margin(book_margins: dict) -> float:
    """
    Cross-book fallback margin: the average of every book-specific margin
    that WAS successfully derived this week, for use only by a book that
    itself lacks enough companion two-sided markets. Still 100%
    market-derived (never a fixed constant) -- just not specific to the
    one book being adjusted. Requires at least FALLBACK_MIN_BOOKS other
    book-specific estimates to exist; returns None otherwise, meaning
    there is no defensible fallback and the caller must omit the book.
    """
    if len(book_margins) < FALLBACK_MIN_BOOKS:
        return None
    return float(sum(v["margin"] for v in book_margins.values()) / len(book_margins))


def _single_sided_fair_prob(yes_price, book_margin: float):
    """
    p_raw = american_to_implied_prob(yes_price)
    p_fair = p_raw / (1 + book_margin)   -- multiplicative/proportional
    devig, the standard method for recovering a fair probability from one
    observed side under an assumed proportional margin structure (the
    same structure already implicit in how every two-sided market in this
    app computes `total = p_a + p_b` = 1+margin before normalizing).
    book_margin is clamped to >=0 (never let a negative estimate inflate
    p_fair above p_raw). Returns None if the price is invalid or the
    result falls outside a valid open probability interval (0, 1) --
    reusing the same bounds discipline as the two-sided path's own
    Poisson guard, never a fabricated value.
    """
    p_raw = american_to_implied_prob(yes_price)
    if p_raw is None:
        return None
    margin = book_margin if (book_margin is not None and book_margin > 0) else 0.0
    p_fair = p_raw / (1 + margin)
    if p_fair is None or p_fair <= 0 or p_fair >= 1:
        return None
    return p_fair


# =======================
# ANYTIME TD -> POISSON FANTASY-POINT CONVERSION
# =======================
def anytime_td_expectations(raw_lines: pd.DataFrame, companion_raw_lines: dict = None) -> pd.DataFrame:
    """
    Mechanical market translation, not a player-specific model. Combines,
    per player, whichever of these apply across the contributing books:

      TWO-SIDED (unchanged from the original approved methodology):
        1. per-book Yes/No devig      (core/pipeline.py::_implied_prob_no_vig, unmodified)
        2. BOOK_WEIGHTS-weighted fair consensus P(TD>=1) across the
           two-sided books ONLY (core/pipeline.py::_consensus_engine,
           unmodified -- called on exactly the two-sided-book subset, so
           when a player has ONLY two-sided books this reduces to
           byte-identical output to before companion_raw_lines existed).

      SINGLE-SIDED (new, additive, only used when companion_raw_lines is
      given and a book posted Yes with no No):
        1. book-specific margin estimate from that SAME book's own other
           two-sided companion props this week (_derive_book_margins),
           falling back to the cross-book average only with sufficient
           evidence (_fallback_margin), else the book is OMITTED --
           never a fixed/hand-set vig percentage.
        2. p_fair = p_raw / (1 + book_margin)   (_single_sided_fair_prob)

      Both kinds of per-book fair probabilities are then combined into
      ONE consensus using the exact same BOOK_WEIGHTS-weighted-average
      principle _consensus_engine already applies -- mathematically, a
      weighted average of (a two-sided sub-consensus, itself already a
      weighted average) and (individual single-sided fair probabilities)
      IS the overall weighted average of every contributing book, since a
      weighted average of weighted averages (weighted by their own total
      weight) equals the combined weighted average of the underlying
      elements.

      3. lambda = -ln(1 - P)
      4. expected_td_points = lambda * 6.0     (core/lineup_data.py::_PTS_PER_TD, reused unchanged)

    Returns columns: player_key, player_display, td_prob, td_lambda,
    td_fantasy_pts, num_books, source ("two-sided-only" / "single-sided-only"
    / "mixed"), contributing_books (list of {book, source, margin_used,
    fair_prob} -- margin_used is None for two-sided books). Groups below
    MIN_BOOKS_FOR_PROP_CONSENSUS (combined two-sided + single-sided book
    count) are excluded, matching every other market's coverage gate.
    """
    empty_cols = ["player_key", "player_display", "td_prob", "td_lambda", "td_fantasy_pts",
                  "num_books", "source", "contributing_books"]
    cfg = PROP_MARKETS["player_anytime_td"]
    books_df = build_prop_books_df(raw_lines, cfg)
    if books_df.empty:
        return pd.DataFrame(columns=empty_cols)

    # ── Two-sided subset: existing devig path, UNCHANGED. ──
    two_sided_df = books_df.dropna(subset=[cfg.price_a_col, cfg.price_b_col])
    two_sided_cons = pd.DataFrame()
    two_sided_weight_by_player: dict = {}
    if not two_sided_df.empty:
        two_sided_cons = _consensus_engine(
            df=two_sided_df, group_keys=["player_key"], side_a_price=cfg.price_a_col, side_b_price=cfg.price_b_col,
            out_a=cfg.fair_a_col, out_b=cfg.fair_b_col, label=cfg.name,
        )
        for pk, grp in two_sided_df.groupby("player_key"):
            two_sided_weight_by_player[pk] = sum(_book_weight(b) for b in grp["book"].unique())

    # ── Single-sided subset: new margin-adjusted path. ──
    single_sided_df = books_df[books_df[cfg.price_a_col].notna() & books_df[cfg.price_b_col].isna()]
    book_margins = _derive_book_margins(companion_raw_lines or {})
    fallback_margin = _fallback_margin(book_margins)

    single_rows_by_player: dict = {}
    if not single_sided_df.empty:
        for _, r in single_sided_df.iterrows():
            book = r["book"]
            margin_info = book_margins.get(book)
            if margin_info is not None and margin_info["n_samples"] >= MIN_MARGIN_SAMPLES:
                margin_used, source = margin_info["margin"], "single-sided-book-margin"
            elif fallback_margin is not None:
                margin_used, source = fallback_margin, "single-sided-cross-book-fallback"
            else:
                continue  # no defensible market-derived margin -- omit this book, never a fixed constant
            fair_p = _single_sided_fair_prob(r[cfg.price_a_col], margin_used)
            if fair_p is None:
                continue
            single_rows_by_player.setdefault(r["player_key"], []).append({
                "book": book, "source": source, "margin_used": round(margin_used, 4), "fair_prob": fair_p,
                "weight": _book_weight(book),
            })

    # ── Combine per player. ──
    display = books_df[["player_key", "player_display"]].drop_duplicates(subset=["player_key"])
    display_by_key = dict(zip(display["player_key"], display["player_display"]))

    all_player_keys = set(two_sided_weight_by_player.keys()) | set(single_rows_by_player.keys())
    out_rows = []
    for pk in all_player_keys:
        contributing_books = []
        num_two_sided = 0
        fair_two_sided, w_two_sided = None, 0.0
        if pk in two_sided_weight_by_player and not two_sided_cons.empty:
            row = two_sided_cons[two_sided_cons["player_key"] == pk]
            if not row.empty:
                fair_two_sided = float(row.iloc[0][cfg.fair_a_col])
                w_two_sided = two_sided_weight_by_player[pk]
                num_two_sided = int(two_sided_df[two_sided_df["player_key"] == pk]["book"].nunique())
                contributing_books.append({
                    "book": ", ".join(sorted(two_sided_df[two_sided_df["player_key"] == pk]["book"].unique())),
                    "source": "two-sided", "margin_used": None, "fair_prob": round(fair_two_sided, 4),
                })

        single_rows = single_rows_by_player.get(pk, [])
        w_single = sum(r["weight"] for r in single_rows)
        for r in single_rows:
            contributing_books.append({
                "book": r["book"], "source": r["source"],
                "margin_used": r["margin_used"], "fair_prob": round(r["fair_prob"], 4),
            })

        total_weight = w_two_sided + w_single
        if total_weight <= 0:
            continue
        numerator = (fair_two_sided * w_two_sided if fair_two_sided is not None else 0.0) + \
                    sum(r["fair_prob"] * r["weight"] for r in single_rows)
        fair_consensus = numerator / total_weight

        num_books = num_two_sided + len(single_rows)
        if num_books < MIN_BOOKS_FOR_PROP_CONSENSUS:
            continue

        if fair_consensus is None or fair_consensus < 0 or fair_consensus >= 1:
            continue
        lam = -math.log(1 - fair_consensus)

        if num_two_sided > 0 and single_rows:
            source = "mixed"
        elif num_two_sided > 0:
            source = "two-sided-only"
        else:
            source = "single-sided-only"

        out_rows.append({
            "player_key": pk, "player_display": display_by_key.get(pk, pk),
            "td_prob": fair_consensus, "td_lambda": lam, "td_fantasy_pts": lam * _PTS_PER_TD,
            "num_books": num_books, "source": source, "contributing_books": contributing_books,
        })

    return pd.DataFrame(out_rows, columns=empty_cols) if out_rows else pd.DataFrame(columns=empty_cols)


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
    companion_raw_lines: dict = {}
    for mkt in _COMPANION_MARKETS:
        raw = _load(mkt)
        companion_raw_lines[mkt] = raw  # reused for single-sided Anytime TD margin estimation below -- no extra fetch
        market_data[mkt] = weighted_market_lines(raw, mkt)
    td_raw = _load("player_anytime_td")
    market_data["player_anytime_td"] = anytime_td_expectations(td_raw, companion_raw_lines)

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
                player_detail[label] = {
                    "prob": r["td_prob"], "lambda": r["td_lambda"], "fantasy_pts": r["td_fantasy_pts"],
                    "num_books": r["num_books"], "source": r.get("source"),
                    "contributing_books": r.get("contributing_books", []),
                }
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
