# core/pipeline.py
# Generic MarketConfig-driven pipeline: raw odds lines -> consensus fair
# value -> best prices -> market intelligence -> display-ready rows.
# One reusable path for Moneyline, Spread, and Total — no per-market
# duplicate functions.
import math
import logging
from dataclasses import dataclass, field as dc_field

import pandas as pd

from core.odds_math import (
    american_to_implied_prob,
    expected_value_pct,
    kelly_fraction,
    fmt_date_et_str,
    fmt_odds,
    fmt_ev,
)

logger = logging.getLogger(__name__)


# =======================
# DATAFRAME VALIDATOR
# =======================
def validate_df(df: pd.DataFrame, label: str, required: list[str] | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    col_counts = {}
    for c in df.columns:
        col_counts[c] = col_counts.get(c, 0) + 1
    dupes = [c for c, n in col_counts.items() if n > 1]
    if dupes:
        raise ValueError(f"[{label}] Duplicate column names: {dupes}\nAll columns: {list(df.columns)}")
    if df.index.duplicated().any():
        raise ValueError(f"[{label}] Duplicate index labels found. Call .reset_index(drop=True) before this point.")
    if required:
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"[{label}] Missing required columns: {missing}\nAvailable: {list(df.columns)}")
    return df


# =======================
# PIPELINE TRACE
# =======================
@dataclass
class PipelineTrace:
    market:          str = ""
    raw:             int = 0
    after_window:    int = 0
    after_build:     int = 0
    after_consensus: int = 0
    after_best:      int = 0
    after_merge:     int = 0
    after_mi:        int = 0
    after_display:   int = 0
    after_filter:    int = 0
    error:           str = ""
    sample_commence: str = ""


# =======================
# MARKET CONFIGURATION
# =======================
@dataclass(frozen=True)
class MarketConfig:
    name:            str
    db_market_key:   str
    odds_api_market: str
    valid_sides:     tuple
    side_a:          str
    side_b:          str
    price_a_col:     str
    price_b_col:     str
    fair_a_col:      str
    fair_b_col:      str
    label_a:         str | None
    label_b:         str | None
    group_keys:      tuple
    line_col:        str | None
    display_line:    bool
    market_label:    str


MARKETS: dict[str, MarketConfig] = {
    "moneyline": MarketConfig(
        name="Moneyline", db_market_key="moneyline", odds_api_market="h2h",
        valid_sides=("home", "away"), side_a="home", side_b="away",
        price_a_col="home_price", price_b_col="away_price",
        fair_a_col="home_fair_prob", fair_b_col="away_fair_prob",
        label_a=None, label_b=None, group_keys=(), line_col=None,
        display_line=False, market_label="Moneyline",
    ),
    "spread": MarketConfig(
        name="Spread", db_market_key="spread", odds_api_market="spreads",
        valid_sides=("home", "away"), side_a="home", side_b="away",
        price_a_col="home_price", price_b_col="away_price",
        fair_a_col="home_fair_prob", fair_b_col="away_fair_prob",
        label_a=None, label_b=None, group_keys=("line",), line_col="line",
        display_line=True, market_label="Spread",
    ),
    "total": MarketConfig(
        name="Total", db_market_key="total", odds_api_market="totals",
        valid_sides=("over", "under"), side_a="over", side_b="under",
        price_a_col="over_price", price_b_col="under_price",
        fair_a_col="over_fair_prob", fair_b_col="under_fair_prob",
        label_a="Over", label_b="Under", group_keys=("total",), line_col="line",
        display_line=True, market_label="Total",
    ),
}


# =======================
# BOOK WEIGHTING
# =======================
BOOK_WEIGHTS: dict = {
    "eu_pinnacle": 1.50, "pinnacle": 1.50,
    "fanduel": 1.00, "draftkings": 1.00,
    "caesars": 0.90, "bet365": 0.90,
    "kalshi": 0.75,
    "betmgm": 0.50, "fanatics": 0.45, "espnbet": 0.45,
    "hardrockbet": 0.40, "betrivers": 0.40, "lowvig": 0.40,
    "ballybet": 0.35, "betparx": 0.35, "betonline": 0.35,
    "fliff": 0.35, "rebet": 0.35, "underdog": 0.35,
    "betanysports": 0.30, "bovada": 0.30, "mybookie": 0.30, "betus": 0.30,
}


def _book_weight(book: str) -> float:
    return BOOK_WEIGHTS.get(book.lower(), 0.30)


# =======================
# CONSENSUS ELIGIBILITY (separate from BOOK_WEIGHTS / ANCHOR_BOOKS)
# =======================
# "US Exchanges" sources (The Odds API's regions=us_ex category --
# prediction-market/exchange venues, structurally different from a
# traditional sportsbook's quote) must be explicitly approved before their
# prices count toward the BOOK_WEIGHTS-weighted fair-value consensus
# (_consensus_engine). This is intentionally separate from BOOK_WEIGHTS
# (a weight) and ANCHOR_BOOKS (used only for the consensus-confidence
# rating's dispersion calc) -- it's a narrower gate answering a different
# question: "may this source's price influence Fair Odds/EV/Kelly at
# all," not "how much."
#
# Every traditional sportsbook -- whether or not it has an explicit
# BOOK_WEIGHTS entry -- is UNAFFECTED by this gate and behaves exactly as
# it did before this section existed: _is_consensus_eligible() returns
# True for anything not in EXCHANGE_BOOKS below, same as always.
#
# An exchange source is eligible ONLY if listed in
# CONSENSUS_ELIGIBLE_EXCHANGE_BOOKS. Kalshi is listed there because it
# already has a deliberately-configured BOOK_WEIGHTS/ANCHOR_BOOKS entry --
# this preserves that existing decision, it doesn't newly grant it.
# Polymarket, Novig, ProphetX, and BetOpenly are NOT listed -- they remain
# fully visible everywhere else (sportsbook filters, best-price/line-
# shopping, the per-game "compare all sportsbooks" table) but contribute
# nothing to Fair Odds, EV%, Kelly, or the consensus-confidence rating
# until a deliberate decision adds them here.
#
# This gate is applied in run_market_pipeline ONLY to the rows passed into
# _consensus_engine -- best_prices() and build_market_intelligence() still
# receive every book unfiltered, which is what keeps an ineligible
# exchange source's own price visible for shopping/comparison. Known
# limitation: if an ineligible source posts the ONLY line at a given
# spread/total value (no eligible book shares that exact line), that one
# line's row won't appear in the final display at all, because
# run_market_pipeline's existing inner-merge between best_prices() and
# _consensus_engine()'s output (unchanged here) requires both to have a
# row for the same group. This is an existing property of that merge, not
# new behavior introduced by this gate, and is expected to be rare for
# game markets (unlike player props, spread/total lines cluster tightly
# around values every major book already posts).
EXCHANGE_BOOKS = {"kalshi", "polymarket", "novig", "prophetx", "betopenly"}
CONSENSUS_ELIGIBLE_EXCHANGE_BOOKS = {"kalshi"}


def _is_consensus_eligible(book: str) -> bool:
    b = str(book).lower()
    if b in EXCHANGE_BOOKS:
        return b in CONSENSUS_ELIGIBLE_EXCHANGE_BOOKS
    return True


# =======================
# GENERIC MARKET BUILDER
# =======================
def build_books_df(df_lines: pd.DataFrame, cfg: MarketConfig) -> pd.DataFrame:
    if df_lines.empty:
        return pd.DataFrame()
    df = df_lines[
        (df_lines["market"] == cfg.odds_api_market) &
        (df_lines["side"].isin(cfg.valid_sides))
    ].copy()
    if df.empty:
        return pd.DataFrame()

    # Spread lines are reported mirrored between home and away (-3.5 / +3.5).
    # Normalize to the home team's sign before grouping.
    if cfg.odds_api_market == "spreads" and "line" in df.columns:
        df["line"] = df.apply(
            lambda r: r["line"] if r["side"] == cfg.side_a else -r["line"], axis=1
        )

    df[cfg.price_a_col] = df.apply(lambda r: r["price"] if r["side"] == cfg.side_a else None, axis=1)
    df[cfg.price_b_col] = df.apply(lambda r: r["price"] if r["side"] == cfg.side_b else None, axis=1)

    base_keys = ["event_id", "home_team", "away_team", "book", "commence_time"]
    if cfg.line_col and cfg.line_col in df.columns:
        raw_group_keys = base_keys + [cfg.line_col]
    elif cfg.group_keys:
        raw_group_keys = base_keys + [k for k in cfg.group_keys if k in df.columns and k not in base_keys]
    else:
        raw_group_keys = base_keys

    agg_dict = {
        cfg.price_a_col: (cfg.price_a_col, "max"),
        cfg.price_b_col: (cfg.price_b_col, "max"),
    }
    result = df.groupby(raw_group_keys, as_index=False).agg(**agg_dict)
    if cfg.odds_api_market == "totals" and "line" in result.columns:
        result = result.rename(columns={"line": "total"})
    return validate_df(
        result.reset_index(drop=True),
        f"build_books_df[{cfg.name}]",
        required=["event_id", "home_team", "away_team", "commence_time", "book",
                  cfg.price_a_col, cfg.price_b_col],
    )


# =======================
# CONSENSUS ENGINE
# =======================
def _implied_prob_no_vig(p_a: float, p_b: float) -> tuple[float, float]:
    total = (p_a or 0.0) + (p_b or 0.0)
    if total <= 0:
        return 0.5, 0.5
    return p_a / total, p_b / total


def _fair_odds_american(fair_prob: float) -> int | None:
    if fair_prob is None or fair_prob <= 0 or fair_prob >= 1:
        return None
    if fair_prob >= 0.5:
        return round(-100 * fair_prob / (1 - fair_prob))
    return round(100 * (1 - fair_prob) / fair_prob)


def _consensus_engine(df, group_keys, side_a_price, side_b_price, out_a, out_b, label) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    validate_df(df, f"_consensus_engine[{label}] input",
                required=group_keys + ["book", side_a_price, side_b_price])
    df = df.copy().reset_index(drop=True)
    df["_imp_a"] = df[side_a_price].apply(american_to_implied_prob)
    df["_imp_b"] = df[side_b_price].apply(american_to_implied_prob)
    df = df.dropna(subset=["_imp_a", "_imp_b"]).reset_index(drop=True)
    if df.empty:
        return pd.DataFrame()

    results = df.apply(lambda r: pd.Series(_implied_prob_no_vig(r["_imp_a"], r["_imp_b"])), axis=1)
    df["_fair_a"] = results[0]
    df["_fair_b"] = results[1]
    df["_weight"] = df["book"].apply(_book_weight)

    _anchor_set = {"eu_pinnacle", "pinnacle", "fanduel", "draftkings", "caesars", "bet365", "kalshi"}

    def _agg_group(g):
        w_sum = g["_weight"].sum()
        fair_a = (g["_fair_a"] * g["_weight"]).sum() / w_sum if w_sum > 0 else 0.5
        fair_b = (g["_fair_b"] * g["_weight"]).sum() / w_sum if w_sum > 0 else 0.5
        total = fair_a + fair_b
        if total > 0:
            fair_a /= total
            fair_b /= total
        return pd.Series({
            out_a: fair_a, out_b: fair_b,
            "num_books": int(g["book"].nunique()),
            "anchor_books_used": ", ".join(sorted(b for b in g["book"].str.lower().unique() if b in _anchor_set)),
        })

    agg = df.groupby(group_keys).apply(_agg_group).reset_index()
    agg[f"{out_a}_odds"] = agg[out_a].apply(_fair_odds_american)
    agg[f"{out_b}_odds"] = agg[out_b].apply(_fair_odds_american)
    out_cols = group_keys + [out_a, out_b, f"{out_a}_odds", f"{out_b}_odds", "num_books", "anchor_books_used"]
    return validate_df(agg[out_cols].reset_index(drop=True), f"_consensus_engine[{label}] output", required=out_cols)


# =======================
# GENERIC BEST PRICES
# =======================
def best_prices(df: pd.DataFrame, cfg: MarketConfig) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    merge_keys = ["event_id"] + list(cfg.group_keys)
    validate_df(df, f"best_prices[{cfg.name}] input",
                required=["event_id", "home_team", "away_team", "commence_time", "book"] + list(cfg.group_keys))

    meta_cols = ["event_id", "home_team", "away_team", "commence_time"] + list(cfg.group_keys)
    a_cols = meta_cols + ["book", cfg.price_a_col]
    side_a = (
        df.dropna(subset=[cfg.price_a_col])
        .sort_values(cfg.price_a_col, ascending=False)
        .groupby(merge_keys, as_index=False).first()[a_cols]
        .rename(columns={"book": f"{cfg.side_a}_book"})
    )
    b_cols = merge_keys + ["book", cfg.price_b_col]
    side_b = (
        df.dropna(subset=[cfg.price_b_col])
        .sort_values(cfg.price_b_col, ascending=False)
        .groupby(merge_keys, as_index=False).first()[b_cols]
        .rename(columns={"book": f"{cfg.side_b}_book"})
    )
    merged = pd.merge(side_a, side_b, on=merge_keys, how="outer", validate="one_to_one")
    return validate_df(merged.reset_index(drop=True), f"best_prices[{cfg.name}] output",
                        required=["event_id", "home_team", "away_team", "commence_time"])


# =======================
# GENERIC DISPLAY BUILDER
# =======================
def build_display_rows(df_in: pd.DataFrame, cfg: MarketConfig, bankroll: float, kf: float) -> pd.DataFrame:
    if df_in.empty:
        return pd.DataFrame()
    sides = [
        (cfg.price_a_col, cfg.fair_a_col, cfg.label_a, f"{cfg.side_a}_book",
         "home_team" if cfg.side_a == "home" else None),
        (cfg.price_b_col, cfg.fair_b_col, cfg.label_b, f"{cfg.side_b}_book",
         "away_team" if cfg.side_b == "away" else None),
    ]
    all_rows = []
    for price_col, fair_col, static_label, book_col, team_col in sides:
        needed = [price_col, fair_col, book_col, "home_team", "away_team", "commence_time"]
        if cfg.display_line:
            line_key = "line" if "line" in df_in.columns else "total"
            needed.append(line_key)
        needed += [c for c in df_in.columns if c.startswith("mi_")]
        sub = df_in[[c for c in needed if c in df_in.columns]].copy()
        sub = sub.dropna(subset=[price_col, fair_col]).reset_index(drop=True)
        sub["_price"] = sub[price_col].astype(int)
        sub["_fair_raw"] = sub[fair_col].astype(float)
        sub["_ev_raw"] = sub.apply(lambda r: expected_value_pct(r["_fair_raw"], r["_price"]), axis=1)
        sub["_kelly"] = sub.apply(lambda r: kelly_fraction(r["_fair_raw"], r["_price"]), axis=1)
        sub["Market"] = cfg.market_label
        sub["Date"] = sub["commence_time"].apply(fmt_date_et_str)
        sub["Game"] = sub["home_team"] + " vs " + sub["away_team"]
        sub["Pick"] = sub[team_col] if (team_col and team_col in sub.columns) else static_label
        sub["Best Odds"] = sub["_price"]
        sub["Best Book"] = sub[book_col]
        sub["Fair Win %"] = sub["_fair_raw"]
        sub["EV%"] = sub["_ev_raw"]
        sub["Kelly (u)"] = sub["_kelly"]
        sub["Stake ($)"] = (bankroll * kf * sub["_kelly"]).round(2)
        if cfg.display_line:
            line_key = "line" if "line" in sub.columns else "total"
            # "line" is normalized to the home side's sign for grouping (see
            # build_books_df) and carried through unchanged to this point, so
            # it's still home's sign here regardless of which side's sub-frame
            # we're building. Flip it back for the away side's own display —
            # mirrors the same negation sportsbook_screener.py already does
            # for its away-side spread label.
            _is_away_side = cfg.odds_api_market == "spreads" and price_col == cfg.price_b_col
            sub["Line"] = sub[line_key].apply(
                lambda l: (
                    (f"{-float(l):+g}" if _is_away_side else f"{float(l):+g}")
                    if cfg.odds_api_market == "spreads" else f"{float(l):.1f}"
                ) if pd.notna(l) else ""
            )
        all_rows.append(sub)
    if not all_rows:
        return pd.DataFrame()
    result = pd.concat(all_rows, ignore_index=True)
    base_cols = ["Market", "Date", "commence_time", "Game", "Pick"]
    if cfg.display_line:
        base_cols.append("Line")
    base_cols += [
        "Best Odds", "Best Book", "Fair Win %", "EV%", "Kelly (u)", "Stake ($)", "_ev_raw", "_fair_raw",
        "mi_rating", "mi_rating_label", "mi_num_books", "mi_num_anchors", "mi_anchor_list", "mi_std_dev",
        "mi_fair_odds_a", "mi_fair_odds_b", "mi_line_diff_a", "mi_line_diff_b", "mi_book_table",
    ]
    return result[[c for c in base_cols if c in result.columns]]


def format_display_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["Best Odds"] = df["Best Odds"].apply(fmt_odds)
    df["Fair Win %"] = (df["Fair Win %"] * 100).map(lambda x: f"{x:.1f}%")
    df["EV%"] = df["EV%"].map(fmt_ev)
    df["Kelly (u)"] = df["Kelly (u)"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
    # _fair_raw (the unrounded fair probability) is kept for callers that
    # need to recompute EV against a filtered subset of books — e.g.
    # tabs/fair_value_model.py — without losing precision to the rounded
    # "Fair Win %" display string. It's still an internal `_`-prefixed
    # column, so display code must not add it to any user-facing table.
    return df.drop(columns=["_ev_raw"], errors="ignore")


# =======================
# MARKET INTELLIGENCE ENGINE
# =======================
ANCHOR_BOOKS = {"eu_pinnacle", "pinnacle", "fanduel", "draftkings", "caesars", "bet365", "kalshi"}
ANCHOR_DISPLAY = {
    "eu_pinnacle": "Pinnacle", "pinnacle": "Pinnacle", "fanduel": "FanDuel",
    "draftkings": "DraftKings", "caesars": "Caesars", "bet365": "bet365", "kalshi": "Kalshi",
}


def _vig_removed_prob(price_a, price_b):
    pa = american_to_implied_prob(price_a)
    pb = american_to_implied_prob(price_b)
    if pa is None or pb is None:
        return None
    total = pa + pb
    return pa / total if total > 0 else None


def compute_dispersion(fair_probs: list[float]) -> tuple[float, float, float]:
    if len(fair_probs) < 2:
        return 0.0, 0.0, 0.0
    n = len(fair_probs)
    mean = sum(fair_probs) / n
    std = math.sqrt(sum((p - mean) ** 2 for p in fair_probs) / n)
    spread = max(fair_probs) - min(fair_probs)
    avg_dev = sum(abs(p - mean) for p in fair_probs) / n
    return round(std, 4), round(spread, 4), round(avg_dev, 4)


def consensus_rating(std_dev: float, num_anchors: int) -> tuple[str, str]:
    if num_anchors < 2:
        return "", "Little"
    if std_dev < 0.005:
        return "", "Very Strong"
    if std_dev < 0.010:
        return "", "Strong"
    if std_dev < 0.020:
        return "", "Moderate"
    if std_dev < 0.035:
        return "", "Weak"
    return "", "Little"


def build_market_intelligence(books_df: pd.DataFrame, merged: pd.DataFrame, cfg: MarketConfig) -> pd.DataFrame:
    if merged.empty or books_df.empty:
        return merged
    merge_keys = ["event_id"] + list(cfg.group_keys)
    price_a_col, price_b_col = cfg.price_a_col, cfg.price_b_col
    fair_a_col, fair_b_col = cfg.fair_a_col, cfg.fair_b_col
    mi_rows = []
    for _, row in merged.iterrows():
        mask = books_df["event_id"] == row["event_id"]
        for gk in cfg.group_keys:
            if gk in books_df.columns and gk in row.index:
                mask &= (books_df[gk] == row[gk])
        game_books = books_df[mask].copy()

        anchor_probs_a, anchor_list, book_table = [], [], []
        best_a_in_game = best_b_in_game = None
        for _, br in game_books.iterrows():
            pa, pb = br.get(price_a_col), br.get(price_b_col)
            if pd.notna(pa):
                best_a_in_game = max(best_a_in_game, int(pa)) if best_a_in_game is not None else int(pa)
            if pd.notna(pb):
                best_b_in_game = max(best_b_in_game, int(pb)) if best_b_in_game is not None else int(pb)
        for _, br in game_books.iterrows():
            book_key = str(br["book"]).lower()
            p_a, p_b = br.get(price_a_col), br.get(price_b_col)
            weight = _book_weight(book_key)
            is_anchor = book_key in ANCHOR_BOOKS
            display_name = ANCHOR_DISPLAY.get(book_key, br["book"])
            fair_a = _vig_removed_prob(p_a, p_b) if pd.notna(p_a) and pd.notna(p_b) else None
            fair_b = (1 - fair_a) if fair_a is not None else None
            is_best_a = pd.notna(p_a) and best_a_in_game is not None and int(p_a) == best_a_in_game
            is_best_b = pd.notna(p_b) and best_b_in_game is not None and int(p_b) == best_b_in_game
            book_table.append({
                "_book_key": book_key, "_weight": weight, "_is_anchor": is_anchor,
                "_is_best_a": is_best_a, "_is_best_b": is_best_b,
                "_fair_a_raw": fair_a, "_fair_b_raw": fair_b,
                "_odds_a_raw": int(p_a) if pd.notna(p_a) else None,
                "_odds_b_raw": int(p_b) if pd.notna(p_b) else None,
                "Sportsbook": display_name,
                "Side A Odds": fmt_odds(int(p_a)) if pd.notna(p_a) else "—",
                "Side B Odds": fmt_odds(int(p_b)) if pd.notna(p_b) else "—",
                "Side A Fair %": f"{fair_a * 100:.1f}%" if fair_a is not None else "—",
                "Side B Fair %": f"{fair_b * 100:.1f}%" if fair_b is not None else "—",
                "Weight": f"{weight:.2f}", "Anchor": "Yes" if is_anchor else "",
            })
            if is_anchor and fair_a is not None:
                anchor_probs_a.append(fair_a)
                anchor_list.append(display_name)

        num_anchors = len(anchor_probs_a)
        std_dev, max_spread, avg_dev = compute_dispersion(anchor_probs_a)
        rating, rating_label = consensus_rating(std_dev, num_anchors)

        fair_a_prob, fair_b_prob = row.get(fair_a_col), row.get(fair_b_col)
        fair_odds_a = _fair_odds_american(fair_a_prob) if fair_a_prob else None
        fair_odds_b = _fair_odds_american(fair_b_prob) if fair_b_prob else None
        best_a_price, best_b_price = row.get(price_a_col), row.get(price_b_col)
        line_diff_a = (int(best_a_price) - fair_odds_a) if (
            best_a_price is not None and pd.notna(best_a_price) and fair_odds_a is not None) else None
        line_diff_b = (int(best_b_price) - fair_odds_b) if (
            best_b_price is not None and pd.notna(best_b_price) and fair_odds_b is not None) else None

        mi_rows.append({
            "event_id": row["event_id"],
            **{gk: row[gk] for gk in cfg.group_keys if gk in row.index},
            "mi_num_books": len(game_books), "mi_num_anchors": num_anchors,
            "mi_anchor_list": ", ".join(sorted(set(anchor_list))),
            "mi_std_dev": std_dev, "mi_max_spread": max_spread, "mi_avg_deviation": avg_dev,
            "mi_rating": rating, "mi_rating_label": rating_label,
            "mi_fair_odds_a": fair_odds_a, "mi_fair_odds_b": fair_odds_b,
            "mi_line_diff_a": line_diff_a, "mi_line_diff_b": line_diff_b, "mi_book_table": book_table,
        })
    if not mi_rows:
        return merged
    mi_df = pd.DataFrame(mi_rows).reset_index(drop=True)
    result = pd.merge(merged, mi_df, on=merge_keys, how="left")
    return result.reset_index(drop=True)


# =======================
# GENERIC PIPELINE RUNNER
# =======================
def run_market_pipeline(raw_lines, cfg, bankroll, kelly, min_ev, min_fair_pct, show_all, trace=None) -> pd.DataFrame:
    if trace is None:
        trace = PipelineTrace(market=cfg.name)
    if raw_lines.empty:
        trace.error = "raw_lines empty on entry"
        return pd.DataFrame()
    trace.raw = len(raw_lines)
    if raw_lines["commence_time"].notna().any():
        trace.sample_commence = str(raw_lines["commence_time"].dropna().iloc[0])
    merge_keys = ["event_id"] + list(cfg.group_keys)
    try:
        books_df = build_books_df(raw_lines, cfg)
        trace.after_build = len(books_df)
        if books_df.empty:
            trace.error = (
                f"build_books_df returned empty. Raw market values: "
                f"{raw_lines['market'].unique().tolist()[:5]}  Raw side values: "
                f"{raw_lines['side'].unique().tolist()[:5]}  Expected market='{cfg.odds_api_market}' "
                f"sides={list(cfg.valid_sides)}"
            )
            return pd.DataFrame()

        # Consensus-eligibility gate (see _is_consensus_eligible above):
        # only affects what's passed into _consensus_engine. best_prices()
        # and build_market_intelligence() below still receive the full,
        # unfiltered books_df, so an ineligible source (e.g. Polymarket)
        # stays fully visible for best-price/line-shopping/comparison --
        # it simply doesn't influence the weighted fair-value calculation.
        consensus_books_df = books_df[books_df["book"].apply(_is_consensus_eligible)].reset_index(drop=True)
        cons = _consensus_engine(
            df=consensus_books_df, group_keys=merge_keys, side_a_price=cfg.price_a_col, side_b_price=cfg.price_b_col,
            out_a=cfg.fair_a_col, out_b=cfg.fair_b_col, label=cfg.name,
        )
        trace.after_consensus = len(cons)
        if cons.empty:
            trace.error = "consensus engine returned empty"
            return pd.DataFrame()

        best = best_prices(books_df, cfg)
        trace.after_best = len(best)
        if best.empty:
            trace.error = "best_prices returned empty"
            return pd.DataFrame()

        merged = pd.merge(best, cons, on=merge_keys, how="inner").reset_index(drop=True)
        trace.after_merge = len(merged)
        if merged.empty:
            trace.error = (
                f"inner merge produced 0 rows. best keys: "
                f"{best[merge_keys].drop_duplicates().shape[0]} unique  cons keys: "
                f"{cons[merge_keys].drop_duplicates().shape[0]} unique"
            )
            return pd.DataFrame()
        validate_df(merged, f"merged[{cfg.name}]",
                    required=["event_id", "home_team", "away_team", "commence_time", cfg.fair_a_col, cfg.fair_b_col])

        merged = build_market_intelligence(books_df, merged, cfg)
        trace.after_mi = len(merged)

        df_disp = build_display_rows(merged, cfg, bankroll, kelly)
        trace.after_display = len(df_disp)
        if df_disp.empty:
            trace.error = "build_display_rows returned empty"
            return pd.DataFrame()

        if not show_all:
            mask = (df_disp["_ev_raw"] >= min_ev) & (df_disp["_fair_raw"] * 100 >= min_fair_pct)
            df_disp = df_disp[mask].copy()
        trace.after_filter = len(df_disp)
        return format_display_df(df_disp)
    except Exception as _e:
        trace.error = f"{type(_e).__name__}: {_e}"
        logger.exception("run_market_pipeline[%s] failed", cfg.name)
        return pd.DataFrame()
