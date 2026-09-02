# core/nfl_prop_pipeline.py
# Player-prop pricing, built on TOP of core/pipeline.py's existing
# generic engine — not a reimplementation of it. _consensus_engine,
# best_prices, build_market_intelligence, and format_display_df are
# imported and called UNMODIFIED; this file only supplies the two
# genuinely prop-specific steps core/pipeline.py's game-market shape
# can't cover as-is:
#   1. the raw per-book pivot (needs "player_key" in the groupby, which
#      core/pipeline.py::build_books_df does not support today), and
#   2. the final "Pick" label, which for a prop is "{Player} Over/Under"
#      rather than a team name.
# Nothing in core/pipeline.py is imported for MODIFICATION — only for
# reuse — so the existing Moneyline/Spread/Total path is fully isolated
# from everything in this file.
import pandas as pd

from core.odds_math import expected_value_pct, kelly_fraction, fmt_date_et_str
from core.pipeline import (
    MarketConfig, validate_df, _consensus_engine, best_prices, build_market_intelligence,
    format_display_df,
)
from core.nfl_prop_market_config import MIN_BOOKS_FOR_PROP_CONSENSUS


def build_prop_books_df(df_lines: pd.DataFrame, cfg: MarketConfig) -> pd.DataFrame:
    """
    Per-book pivot for player-prop lines — the prop analog of
    core/pipeline.py::build_books_df. df_lines is expected to already
    carry a "player_key" column (normalized identity) alongside the
    same event_id/home_team/away_team/commence_time/book/market/side/
    line/price shape core/data_sources.py's game-market lines use.
    Rows with no player_key (normalize_player_key returned None at
    ingestion time) must already be excluded upstream — this function
    does not itself decide ambiguity, it only groups what it's given.

    Grouping key: event_id, book, player_key, line — this is exactly
    what prevents two different players (different player_key) from
    merging into the same market, and what keeps two different lines
    for the same player as separate markets, mirroring how Spread keeps
    each distinct line separate today.
    """
    if df_lines.empty:
        return pd.DataFrame()
    required_cols = {"event_id", "home_team", "away_team", "commence_time", "book",
                      "market", "side", "line", "price", "player_key", "player_display"}
    missing = required_cols - set(df_lines.columns)
    if missing:
        raise ValueError(f"build_prop_books_df: missing required columns: {sorted(missing)}")

    df = df_lines[
        (df_lines["market"] == cfg.odds_api_market) &
        (df_lines["side"].isin(cfg.valid_sides)) &
        df_lines["player_key"].notna()
    ].copy()
    if df.empty:
        return pd.DataFrame()

    df[cfg.price_a_col] = df.apply(lambda r: r["price"] if r["side"] == cfg.side_a else None, axis=1)
    df[cfg.price_b_col] = df.apply(lambda r: r["price"] if r["side"] == cfg.side_b else None, axis=1)

    group_cols = ["event_id", "home_team", "away_team", "commence_time", "book", "player_key", "line"]
    result = (
        df.groupby(group_cols, as_index=False)
        .agg(**{
            cfg.price_a_col: (cfg.price_a_col, "max"),
            cfg.price_b_col: (cfg.price_b_col, "max"),
            # A player_key can appear with multiple raw display-name
            # spellings across books ("Pat Mahomes" vs "Patrick
            # Mahomes") — keep the first seen as the display label; the
            # grouping identity itself is always player_key, never the
            # display string, so this choice never affects which rows
            # merge, only which spelling shows in the UI.
            "player_display": ("player_display", "first"),
        })
    )
    return validate_df(
        result.reset_index(drop=True), f"build_prop_books_df[{cfg.name}]",
        required=["event_id", "home_team", "away_team", "commence_time", "book",
                  "player_key", "player_display", "line", cfg.price_a_col, cfg.price_b_col],
    )


def build_prop_display_rows(df_in: pd.DataFrame, cfg: MarketConfig, bankroll: float, kf: float) -> pd.DataFrame:
    """
    Prop analog of core/pipeline.py::build_display_rows. Reuses the
    exact same EV%/Kelly formulas (expected_value_pct, kelly_fraction —
    imported unmodified) against the exact same _fair_raw/_price inputs;
    the only real difference from the game-market version is how "Pick"
    is composed (player + side, not a team name).
    """
    if df_in.empty:
        return pd.DataFrame()
    # book_col mirrors core/pipeline.py::build_display_rows exactly:
    # best_prices() (imported, unmodified) names these columns
    # "{side_a}_book"/"{side_b}_book" — "over_book"/"under_book" here.
    sides = [
        (cfg.price_a_col, cfg.fair_a_col, cfg.label_a, f"{cfg.side_a}_book"),
        (cfg.price_b_col, cfg.fair_b_col, cfg.label_b, f"{cfg.side_b}_book"),
    ]
    all_rows = []
    for price_col, fair_col, side_label, book_col in sides:
        needed = [price_col, fair_col, book_col, "player_key", "player_display", "line",
                  "home_team", "away_team", "commence_time", "event_id"]
        needed += [c for c in df_in.columns if c.startswith("mi_")]
        sub = df_in[[c for c in needed if c in df_in.columns]].copy()
        sub = sub.dropna(subset=[price_col, fair_col]).reset_index(drop=True)
        if sub.empty:
            continue
        sub["_price"] = sub[price_col].astype(int)
        sub["_fair_raw"] = sub[fair_col].astype(float)
        sub["_ev_raw"] = sub.apply(lambda r: expected_value_pct(r["_fair_raw"], r["_price"]), axis=1)
        sub["_kelly"] = sub.apply(lambda r: kelly_fraction(r["_fair_raw"], r["_price"]), axis=1)
        sub["Market"] = cfg.market_label
        sub["Date"] = sub["commence_time"].apply(fmt_date_et_str)
        sub["Game"] = sub["home_team"] + " vs " + sub["away_team"]
        sub["Player"] = sub["player_display"]
        sub["Side"] = side_label
        sub["Pick"] = sub["player_display"] + " " + side_label
        sub["Line"] = sub["line"].apply(lambda l: f"{float(l):g}" if pd.notna(l) else "")
        sub["Best Odds"] = sub["_price"]
        sub["Best Book"] = sub[book_col] if book_col in sub.columns else None
        sub["Fair Win %"] = sub["_fair_raw"]
        sub["EV%"] = sub["_ev_raw"]
        sub["Kelly (u)"] = sub["_kelly"]
        sub["Stake ($)"] = (bankroll * kf * sub["_kelly"]).round(2)
        all_rows.append(sub)
    if not all_rows:
        return pd.DataFrame()
    result = pd.concat(all_rows, ignore_index=True)
    base_cols = [
        "Market", "Date", "commence_time", "Game", "Player", "Side", "Pick", "Line",
        "Best Odds", "Best Book", "Fair Win %", "EV%", "Kelly (u)", "Stake ($)", "_ev_raw", "_fair_raw",
        "mi_rating", "mi_rating_label", "mi_num_books", "mi_num_anchors", "mi_anchor_list", "mi_std_dev",
        "mi_fair_odds_a", "mi_fair_odds_b",
    ]
    return result[[c for c in base_cols if c in result.columns]]


def run_prop_market_pipeline(raw_lines: pd.DataFrame, cfg: MarketConfig, bankroll: float, kelly: float,
                              min_ev: float, min_fair_pct: float, show_all: bool) -> pd.DataFrame:
    """
    Player-prop analog of core/pipeline.py::run_market_pipeline. Every
    stage after the initial per-book pivot (_consensus_engine,
    best_prices, build_market_intelligence, format_display_df) is the
    SAME function object imported from core/pipeline.py — this function
    does not reimplement devig, weighting, consensus, dispersion,
    anchor-book logic, EV%, or Kelly anywhere.
    """
    if raw_lines.empty:
        return pd.DataFrame()

    merge_keys = ["event_id"] + list(cfg.group_keys)
    books_df = build_prop_books_df(raw_lines, cfg)
    if books_df.empty:
        return pd.DataFrame()

    cons = _consensus_engine(
        df=books_df, group_keys=merge_keys, side_a_price=cfg.price_a_col, side_b_price=cfg.price_b_col,
        out_a=cfg.fair_a_col, out_b=cfg.fair_b_col, label=cfg.name,
    )
    if cons.empty:
        return pd.DataFrame()

    best = best_prices(books_df, cfg)
    if best.empty:
        return pd.DataFrame()

    merged = pd.merge(best, cons, on=merge_keys, how="inner").reset_index(drop=True)
    if merged.empty:
        return pd.DataFrame()

    # best_prices() (imported, unmodified) only carries cfg.group_keys
    # through its own meta_cols — "player_display" isn't one of those
    # (only "player_key" is, since that's the actual grouping identity),
    # so it's re-attached here from books_df rather than by changing
    # core/pipeline.py's generic meta_cols list for every market.
    player_names = books_df[["player_key", "player_display"]].drop_duplicates(subset=["player_key"])
    merged = merged.merge(player_names, on="player_key", how="left")

    merged = build_market_intelligence(books_df, merged, cfg)

    # Sportsbook-coverage gate: exclude groups below the minimum book
    # count BEFORE display, rather than showing a Fair Price computed
    # from a single sportsbook as if it were a real consensus. Reuses
    # mi_num_books, already computed unmodified by build_market_intelligence
    # above — no new confidence methodology introduced.
    if "mi_num_books" in merged.columns:
        merged = merged[merged["mi_num_books"] >= MIN_BOOKS_FOR_PROP_CONSENSUS].reset_index(drop=True)
    if merged.empty:
        return pd.DataFrame()

    df_disp = build_prop_display_rows(merged, cfg, bankroll, kelly)
    if df_disp.empty:
        return pd.DataFrame()

    if not show_all:
        mask = (df_disp["_ev_raw"] >= min_ev) & (df_disp["_fair_raw"] * 100 >= min_fair_pct)
        df_disp = df_disp[mask].copy()
    if df_disp.empty:
        return pd.DataFrame()

    return format_display_df(df_disp)
