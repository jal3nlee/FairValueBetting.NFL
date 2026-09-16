# core/nfl_provider_diagnostics.py
# TEMPORARY, internal-only diagnostics for verifying The Odds API's actual
# provider behavior for NFL player props (Anytime TD Yes/No coverage,
# regions=us2, Kalshi/us_ex) directly from the deployed app's own
# ODDS_API_KEY -- exactly the checks previously run as one-off /tmp
# scripts via the Render shell, now exposed behind a button in
# tabs/fantasy_rankings.py's "Provider Diagnostics" expander so they can
# run without manual script execution.
#
# This module is NOT part of ingestion, NOT a data source for any
# customer-facing feature, and introduces NO methodology of its own --
# it only reads live Odds API responses and reports on them using the
# exact same pipeline functions Fantasy Rankings/FVM already use
# unmodified (core/pipeline.py::_consensus_engine, core/odds_math.py::
# american_to_implied_prob, core/nfl_prop_pipeline.py::build_prop_books_df).
# Every network call here is a plain HTTP GET to The Odds API -- no writes
# anywhere, to Supabase or otherwise. Every call is gated behind an
# explicit button click in the UI layer (never fired on a bare rerun) and
# the UI wraps the entry point below in @st.cache_data(ttl=...) so repeat
# clicks within the cache window don't re-spend API credits.
#
# ODDS_API_KEY is read via os.getenv, the same convention already used by
# fetch_odds_nfl_props.py and core/lineup_data.py -- never logged, never
# included in any returned dict, never displayed.
import os

import pandas as pd
import requests

from core.pipeline import _consensus_engine, MarketConfig
from core.odds_math import american_to_implied_prob
from core.nfl_prop_market_config import PROP_MARKETS
from core.nfl_prop_pipeline import build_prop_books_df

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_SPORT_KEY = "americanfootball_nfl"
REQUEST_TIMEOUT = 20

ALL_MARKETS = ["player_pass_yds", "player_pass_tds", "player_rush_yds",
               "player_reception_yds", "player_receptions",
               "player_pass_interceptions", "player_anytime_td"]
FLAG_BOOKS = ["draftkings", "fanduel", "betmgm", "williamhill_us"]

# Candidate two-sided Over/Under TD-count markets being evaluated as a
# possible replacement for the currently Yes-only player_anytime_td --
# "Over 0.5" on either is the same underlying event as "Anytime TD: Yes".
# player_tds counts ALL touchdown types (including passing -- NOT
# semantically equivalent for a QB without double-counting passing TDs);
# player_rush_reception_tds excludes passing entirely, making it the
# semantically correct candidate if either has real two-sided coverage.
# Neither is in core/nfl_prop_market_config.py::PROP_MARKETS -- this is
# investigation only, nothing is ingested or wired into production.
TD_ALT_MARKETS = ["player_tds", "player_rush_reception_tds"]
TD_ALT_LABELS = {"player_tds": "Touchdowns (all types)", "player_rush_reception_tds": "Rush + Reception TDs"}

# Local, throwaway MarketConfig instances -- used ONLY to prove these
# markets can flow through the existing, UNMODIFIED build_prop_books_df /
# _consensus_engine pipeline (the "existing devig compatibility" check).
# Never added to core/nfl_prop_market_config.py::PROP_MARKETS, never
# touches ingestion, FVM, or Parlay Builder.
TD_ALT_CFG = {
    mkt: MarketConfig(
        name=f"{TD_ALT_LABELS[mkt]} (diagnostic)", db_market_key=mkt, odds_api_market=mkt,
        valid_sides=("over", "under"), side_a="over", side_b="under",
        price_a_col="over_price", price_b_col="under_price",
        fair_a_col="over_fair_prob", fair_b_col="under_fair_prob",
        label_a="Over", label_b="Under", group_keys=("player_key", "line"), line_col="line",
        display_line=True, market_label=TD_ALT_LABELS[mkt],
    )
    for mkt in TD_ALT_MARKETS
}

# One combined request per scenario in production would cost
# (unique markets returned) x (regions requested); this diagnostic makes
# 4 separate single-scope requests instead (to isolate what each region
# contributes on its own), each up to len(ALL_MARKETS)=7 credits under
# the event-odds endpoint's documented "markets returned x regions"
# formula -- worst case ~28 credits total, one-time per click (cached
# afterward, see tabs/fantasy_rankings.py), PLUS one small additional
# regions=us request for the 2 TD_ALT_MARKETS (up to 2 more credits).
ESTIMATED_MAX_CREDITS = 30


def _fetch(event_id: str, regions: str = None, bookmakers: str = None, markets: list = None):
    """One read-only GET to the Odds API event-odds endpoint. Returns
    (status_code, payload_or_None, error_message_or_None). Never raises;
    a network failure is reported back as a clean message, not a
    traceback, and never includes ODDS_API_KEY's value."""
    if not ODDS_API_KEY:
        return None, None, "ODDS_API_KEY is not configured in this environment."
    url = f"https://api.the-odds-api.com/v4/sports/{ODDS_API_SPORT_KEY}/events/{event_id}/odds"
    params = {"apiKey": ODDS_API_KEY, "markets": ",".join(markets or ALL_MARKETS), "oddsFormat": "american"}
    if regions:
        params["regions"] = regions
    if bookmakers:
        params["bookmakers"] = bookmakers
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        return None, None, f"Request failed: {type(e).__name__}"
    if resp.status_code != 200:
        return resp.status_code, None, f"Provider returned HTTP {resp.status_code}"
    try:
        return resp.status_code, resp.json(), None
    except ValueError:
        return resp.status_code, None, "Provider response was not valid JSON"


def _by_player_sides(outcomes: list) -> dict:
    by_player: dict = {}
    for o in outcomes:
        by_player.setdefault(o.get("description"), set()).add((o.get("name") or "").strip().lower())
    return by_player


def _anytime_td_book_breakdown(payload: dict, flag_books: list = None) -> pd.DataFrame:
    """One row per bookmaker that returned player_anytime_td in this
    payload: player/both/yes-only/no-only counts, plus a raw ungrouped
    outcome count and a distinct (description, side) pair count -- if
    "Total Outcomes" == "Players", each player has exactly one outcome
    (genuinely single-sided data). If "Distinct (desc,side) pairs" is
    LARGER than "Players", some outcomes for the same real player are
    grouping under different description keys (a description-formatting
    mismatch, not single-sided data) -- this distinguishes the two
    possible explanations directly rather than by inference."""
    flag_books = set(flag_books or [])
    rows = []
    if not payload:
        return pd.DataFrame(columns=["Bookmaker", "Players", "Both Yes+No", "Yes only", "No only",
                                      "Total Outcomes", "Distinct (desc,side) pairs",
                                      "Raw 'Yes' count", "Raw 'No' count", "Flagged"])
    for book in payload.get("bookmakers", []):
        book_key = book.get("key")
        for m in book.get("markets", []):
            if m.get("key") != "player_anytime_td":
                continue
            outcomes = m.get("outcomes", [])
            by_player = _by_player_sides(outcomes)
            both = sum(1 for s in by_player.values() if {"yes", "no"}.issubset(s))
            yes_only = sum(1 for s in by_player.values() if "yes" in s and "no" not in s)
            no_only = sum(1 for s in by_player.values() if "no" in s and "yes" not in s)
            distinct_pairs = {(o.get("description"), (o.get("name") or "").strip().lower()) for o in outcomes}
            # Zero-grouping cross-check: literal occurrence count of the
            # raw side string, bypassing _by_player_sides entirely -- if
            # this disagrees with yes_only/no_only above, the bug is in
            # the grouping step; if it agrees, the "single-sided" result
            # is real and the bug (if any) is upstream of this function.
            raw_yes_count = sum(1 for o in outcomes if (o.get("name") or "").strip().lower() == "yes")
            raw_no_count = sum(1 for o in outcomes if (o.get("name") or "").strip().lower() == "no")
            rows.append({
                "Bookmaker": book_key, "Players": len(by_player), "Both Yes+No": both,
                "Yes only": yes_only, "No only": no_only,
                "Total Outcomes": len(outcomes), "Distinct (desc,side) pairs": len(distinct_pairs),
                "Raw 'Yes' count": raw_yes_count, "Raw 'No' count": raw_no_count,
                "Flagged": "Yes" if book_key in flag_books else "",
            })
    return pd.DataFrame(rows)


def _anytime_td_raw_rows(payload: dict, event: dict) -> list:
    """Flattens one payload's player_anytime_td outcomes into raw-line
    dicts, the same shape fetch_odds_nfl_props.py itself would store."""
    out = []
    if not payload:
        return out
    for book in payload.get("bookmakers", []):
        book_key = book.get("key")
        for m in book.get("markets", []):
            if m.get("key") != "player_anytime_td":
                continue
            for o in m.get("outcomes", []):
                pname = o.get("description")
                out.append({
                    "event_id": event.get("event_id"), "home_team": event.get("home_team"),
                    "away_team": event.get("away_team"), "commence_time": event.get("commence_time"),
                    "book": book_key, "market": "player_anytime_td",
                    "side": (o.get("name") or "").strip().lower(), "line": o.get("point"),
                    "price": o.get("price"), "player_key": (pname or "").lower(), "player_display": pname,
                })
    return out


def _raw_outcome_sample(payload: dict, book_key_filter: str = None, limit: int = 12) -> pd.DataFrame:
    """Verbatim, unprocessed outcome rows for player_anytime_td -- no
    lowercasing, no grouping, no aggregation -- so the literal provider
    response shape can be inspected directly when the aggregated
    breakdown looks surprising. If book_key_filter is given, only that
    book's outcomes are sampled; otherwise the first book with any
    player_anytime_td outcomes is used."""
    cols = ["Bookmaker", "name (raw)", "description (raw)", "price", "point"]
    if not payload:
        return pd.DataFrame(columns=cols)
    rows = []
    for book in payload.get("bookmakers", []):
        book_key = book.get("key")
        if book_key_filter and book_key != book_key_filter:
            continue
        for m in book.get("markets", []):
            if m.get("key") != "player_anytime_td":
                continue
            for o in m.get("outcomes", []):
                rows.append({
                    "Bookmaker": book_key, "name (raw)": o.get("name"),
                    "description (raw)": o.get("description"), "price": o.get("price"), "point": o.get("point"),
                })
                if len(rows) >= limit:
                    return pd.DataFrame(rows, columns=cols)
        if rows and not book_key_filter:
            break  # got a full sample from the first book that had any -- stop there
    return pd.DataFrame(rows, columns=cols)


def _kalshi_market_coverage(payload: dict) -> pd.DataFrame:
    """One row per one of the 7 markets, reporting whether Kalshi
    returned it in this payload and the shape of what it returned."""
    cols = ["Market", "Returned by Kalshi", "Outcomes", "Players", "Sides seen", "Sample point"]
    if not payload:
        return pd.DataFrame(columns=cols)
    books = [b for b in payload.get("bookmakers", []) if b.get("key") == "kalshi"]
    rows = []
    kb = books[0] if books else None
    for mkt_key in ALL_MARKETS:
        label = PROP_MARKETS[mkt_key].market_label
        markets_found = [m for m in kb.get("markets", [])] if kb else []
        markets_found = [m for m in markets_found if m.get("key") == mkt_key]
        if not markets_found:
            rows.append({"Market": label, "Returned by Kalshi": "No", "Outcomes": 0, "Players": 0,
                         "Sides seen": "", "Sample point": ""})
            continue
        m = markets_found[0]
        outcomes = m.get("outcomes", [])
        by_player = _by_player_sides(outcomes)
        sample = outcomes[0] if outcomes else {}
        rows.append({
            "Market": label, "Returned by Kalshi": "Yes", "Outcomes": len(outcomes),
            "Players": len(by_player), "Sides seen": ", ".join(sorted({s for v in by_player.values() for s in v})),
            "Sample point": sample.get("point"),
        })
    return pd.DataFrame(rows)


def _two_sided_viability(raw_rows: list) -> dict:
    """Distinct players with 1+/2+/3+ books posting BOTH Yes and No for
    the same player from the same book -- the decision metric for
    whether the existing two-sided devig methodology already has enough
    coverage, unmodified from anywhere else in this codebase."""
    if not raw_rows:
        return {"total_players": 0, "n_1plus": 0, "n_2plus": 0, "n_3plus": 0}
    df = pd.DataFrame(raw_rows)
    counts = {}
    for pk in df["player_key"].unique():
        sub = df[df["player_key"] == pk]
        both_books = set(sub[sub["side"] == "yes"]["book"]) & set(sub[sub["side"] == "no"]["book"])
        counts[pk] = len(both_books)
    return {
        "total_players": len(counts),
        "n_1plus": sum(1 for n in counts.values() if n >= 1),
        "n_2plus": sum(1 for n in counts.values() if n >= 2),
        "n_3plus": sum(1 for n in counts.values() if n >= 3),
    }


def _drop_point_trace(raw_rows: list) -> dict:
    """Finds one real Yes-only player/book pair (a book that posted Yes
    for a player but never posted No for that same player) and traces it
    through build_prop_books_df -> implied probabilities ->
    _consensus_engine, all UNMODIFIED. Returns None if every Yes row in
    this response already had a matching No row from the same book."""
    if not raw_rows:
        return None
    df = pd.DataFrame(raw_rows)
    yes_rows = df[df["side"] == "yes"]
    no_rows = df[df["side"] == "no"]
    example = None
    for _, r in yes_rows.iterrows():
        has_no = not no_rows[(no_rows["player_key"] == r["player_key"]) & (no_rows["book"] == r["book"])].empty
        if not has_no:
            example = r
            break
    if example is None:
        return None

    pk, book = example["player_key"], example["book"]
    cfg = PROP_MARKETS["player_anytime_td"]
    books_df = build_prop_books_df(df, cfg)
    row = books_df[(books_df["player_key"] == pk) & (books_df["book"] == book)]
    result = {
        "player": example.get("player_display") or pk, "book": book,
        "yes_price": None, "no_price": None, "implied_yes": None, "implied_no": None,
        "survives_dropna": False, "in_final_consensus": False,
    }
    if row.empty:
        return result
    yes_price, no_price = row.iloc[0][cfg.price_a_col], row.iloc[0][cfg.price_b_col]
    imp_a = american_to_implied_prob(yes_price) if pd.notna(yes_price) else None
    imp_b = american_to_implied_prob(no_price) if pd.notna(no_price) else None
    result.update({
        "yes_price": yes_price if pd.notna(yes_price) else None,
        "no_price": no_price if pd.notna(no_price) else None,
        "implied_yes": round(imp_a, 4) if imp_a is not None else None,
        "implied_no": round(imp_b, 4) if imp_b is not None else None,
        "survives_dropna": imp_a is not None and imp_b is not None,
    })
    cons = _consensus_engine(
        df=books_df, group_keys=["player_key"], side_a_price=cfg.price_a_col, side_b_price=cfg.price_b_col,
        out_a=cfg.fair_a_col, out_b=cfg.fair_b_col, label="Anytime TD (diagnostic trace)",
    )
    result["in_final_consensus"] = (not cons.empty) and (pk in set(cons["player_key"]))
    return result


def _ou_half_line_breakdown(payload: dict, market_key: str, flag_books: list = None) -> pd.DataFrame:
    """One row per bookmaker that returned market_key in this payload:
    total players (any line), the distinct line values seen, and --
    restricted specifically to the 0.5 line, the one structurally
    equivalent to 'scores at least one TD' -- players/both/over-only/
    under-only counts, mirroring _anytime_td_book_breakdown's shape for
    the Yes/No market."""
    flag_books = set(flag_books or [])
    cols = ["Bookmaker", "Players (any line)", "Line values seen", "Players @ 0.5 line",
            "Both O/U @ 0.5", "Over only @ 0.5", "Under only @ 0.5", "Flagged"]
    if not payload:
        return pd.DataFrame(columns=cols)
    rows = []
    for book in payload.get("bookmakers", []):
        book_key = book.get("key")
        for m in book.get("markets", []):
            if m.get("key") != market_key:
                continue
            outcomes = m.get("outcomes", [])
            all_players = {o.get("description") for o in outcomes}
            lines_seen = sorted({o.get("point") for o in outcomes if o.get("point") is not None})
            half_line_players: dict = {}
            for o in outcomes:
                if o.get("point") != 0.5:
                    continue
                half_line_players.setdefault(o.get("description"), set()).add((o.get("name") or "").strip().lower())
            both = sum(1 for s in half_line_players.values() if {"over", "under"}.issubset(s))
            over_only = sum(1 for s in half_line_players.values() if "over" in s and "under" not in s)
            under_only = sum(1 for s in half_line_players.values() if "under" in s and "over" not in s)
            rows.append({
                "Bookmaker": book_key, "Players (any line)": len(all_players),
                "Line values seen": lines_seen, "Players @ 0.5 line": len(half_line_players),
                "Both O/U @ 0.5": both, "Over only @ 0.5": over_only, "Under only @ 0.5": under_only,
                "Flagged": "Yes" if book_key in flag_books else "",
            })
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


def _ou_raw_sample(payload: dict, market_key: str, limit: int = 10) -> pd.DataFrame:
    """Verbatim, unprocessed outcome rows for market_key -- no filtering,
    no grouping -- so the literal provider response shape (description,
    side, point, price) can be inspected directly."""
    cols = ["Bookmaker", "description (raw)", "name (raw)", "point", "price"]
    if not payload:
        return pd.DataFrame(columns=cols)
    rows = []
    for book in payload.get("bookmakers", []):
        book_key = book.get("key")
        for m in book.get("markets", []):
            if m.get("key") != market_key:
                continue
            for o in m.get("outcomes", []):
                rows.append({
                    "Bookmaker": book_key, "description (raw)": o.get("description"),
                    "name (raw)": o.get("name"), "point": o.get("point"), "price": o.get("price"),
                })
                if len(rows) >= limit:
                    return pd.DataFrame(rows, columns=cols)
    return pd.DataFrame(rows, columns=cols)


def _ou_raw_rows(payload: dict, market_key: str, event: dict) -> list:
    """Flattens one payload's market_key outcomes into raw-line dicts,
    the same shape the existing prop pipeline already expects (event_id/
    home_team/away_team/commence_time/book/market/side/line/price/
    player_key/player_display) -- so they can be run through
    build_prop_books_df/_consensus_engine UNMODIFIED."""
    out = []
    if not payload:
        return out
    for book in payload.get("bookmakers", []):
        book_key = book.get("key")
        for m in book.get("markets", []):
            if m.get("key") != market_key:
                continue
            for o in m.get("outcomes", []):
                pname = o.get("description")
                out.append({
                    "event_id": event.get("event_id"), "home_team": event.get("home_team"),
                    "away_team": event.get("away_team"), "commence_time": event.get("commence_time"),
                    "book": book_key, "market": market_key,
                    "side": (o.get("name") or "").strip().lower(), "line": o.get("point"),
                    "price": o.get("price"), "player_key": (pname or "").lower(), "player_display": pname,
                })
    return out


def _ou_two_sided_viability(raw_rows: list) -> dict:
    """Distinct players with 1+/2+/3+ books posting BOTH Over and Under
    AT THE 0.5 LINE for the same player from the same book -- the
    decision metric for whether this market has usable two-sided
    coverage at the line equivalent to 'scores at least one TD'."""
    if not raw_rows:
        return {"total_players": 0, "n_1plus": 0, "n_2plus": 0, "n_3plus": 0}
    df = pd.DataFrame(raw_rows)
    half = df[df["line"] == 0.5]
    if half.empty:
        return {"total_players": 0, "n_1plus": 0, "n_2plus": 0, "n_3plus": 0}
    counts = {}
    for pk in half["player_key"].unique():
        sub = half[half["player_key"] == pk]
        both_books = set(sub[sub["side"] == "over"]["book"]) & set(sub[sub["side"] == "under"]["book"])
        counts[pk] = len(both_books)
    return {
        "total_players": len(counts),
        "n_1plus": sum(1 for n in counts.values() if n >= 1),
        "n_2plus": sum(1 for n in counts.values() if n >= 2),
        "n_3plus": sum(1 for n in counts.values() if n >= 3),
    }


def _ou_devig_compat_check(raw_rows: list, market_key: str) -> dict:
    """Picks one real player with a genuine two-sided 0.5-line Over+Under
    pair (if any exists) and runs it through build_prop_books_df ->
    _consensus_engine -- the EXACT, UNMODIFIED functions every other
    market in this app already uses -- to prove (or disprove) that this
    market's data can flow through the existing devig pipeline with zero
    changes to that pipeline. Returns None if no such pair exists in this
    response at all."""
    if not raw_rows:
        return None
    df = pd.DataFrame(raw_rows)
    half = df[df["line"] == 0.5]
    if half.empty:
        return None
    over_rows = half[half["side"] == "over"]
    under_rows = half[half["side"] == "under"]
    example_pk, example_book = None, None
    for _, r in over_rows.iterrows():
        match = under_rows[(under_rows["player_key"] == r["player_key"]) & (under_rows["book"] == r["book"])]
        if not match.empty:
            example_pk, example_book = r["player_key"], r["book"]
            break
    if example_pk is None:
        return {"found_two_sided_example": False}

    cfg = TD_ALT_CFG[market_key]
    books_df = build_prop_books_df(half, cfg)
    cons = _consensus_engine(
        df=books_df, group_keys=["player_key", "line"], side_a_price=cfg.price_a_col, side_b_price=cfg.price_b_col,
        out_a=cfg.fair_a_col, out_b=cfg.fair_b_col, label=f"{market_key} (diagnostic)",
    )
    row = cons[cons["player_key"] == example_pk]
    return {
        "found_two_sided_example": True, "player_key": example_pk, "book": example_book,
        "reached_build_prop_books_df": not books_df.empty,
        "reached_consensus_engine": not cons.empty,
        "fair_over_prob": round(float(row.iloc[0][cfg.fair_a_col]), 4) if not row.empty else None,
        "fair_under_prob": round(float(row.iloc[0][cfg.fair_b_col]), 4) if not row.empty else None,
    }


def _get_event_meta(supabase, event_id: str) -> dict:
    """Read-only lookup of one event's home_team/away_team/commence_time
    from the existing player_prop_snapshots table -- the same table
    Fantasy Rankings already reads from, no new table/schema, no writes."""
    try:
        res = (
            supabase.table("player_prop_snapshots")
            .select("event_id,home_team,away_team,commence_time")
            .eq("event_id", event_id).limit(1).execute()
        )
        rows = res.data or []
        if rows:
            return rows[0]
    except Exception:
        pass
    return {"event_id": event_id, "home_team": None, "away_team": None, "commence_time": None}


def run_provider_diagnostics(supabase, event_id: str) -> dict:
    """
    Entry point. Looks up event_id's home_team/away_team/commence_time
    (read-only, from the same player_prop_snapshots table Fantasy
    Rankings already reads), then makes exactly 5 live Odds API requests:
    4 for the 7 current prop markets (regions=us, regions=us2, regions=
    us_ex, bookmakers=kalshi) plus 1 additional small request (regions=us
    only) for the 2 candidate two-sided TD-count markets (TD_ALT_MARKETS)
    being evaluated as a possible replacement for the currently Yes-only
    player_anytime_td. Returns a dict of small DataFrames/values ready
    for display -- never raw JSON, never a secret value.
    """
    event = _get_event_meta(supabase, event_id)
    out = {"event": event, "errors": []}

    status_us, payload_us, err_us = _fetch(event_id, regions="us")
    status_us2, payload_us2, err_us2 = _fetch(event_id, regions="us2")
    status_usex, payload_usex, err_usex = _fetch(event_id, regions="us_ex")
    status_kalshi, payload_kalshi, err_kalshi = _fetch(event_id, bookmakers="kalshi")

    for label, err in [("regions=us", err_us), ("regions=us2", err_us2),
                        ("regions=us_ex", err_usex), ("bookmakers=kalshi", err_kalshi)]:
        if err:
            out["errors"].append(f"{label}: {err}")

    out["us_breakdown"] = _anytime_td_book_breakdown(payload_us, FLAG_BOOKS)
    out["us2_breakdown"] = _anytime_td_book_breakdown(payload_us2)
    # Verbatim raw sample -- prefer draftkings (most commonly scrutinized
    # book) if present, else whichever book returned anything, so an
    # aggregated count that looks surprising can be checked against the
    # literal provider response with no processing in between.
    out["raw_sample"] = _raw_outcome_sample(payload_us, book_key_filter="draftkings")
    if out["raw_sample"].empty:
        out["raw_sample"] = _raw_outcome_sample(payload_us)

    raw_us = _anytime_td_raw_rows(payload_us, event)
    raw_us2 = _anytime_td_raw_rows(payload_us2, event)
    us_players = {r["player_key"] for r in raw_us}
    us2_players = {r["player_key"] for r in raw_us2}
    us_books = {r["book"] for r in raw_us}
    us2_books = {r["book"] for r in raw_us2}
    out["us2_summary"] = {
        "total_players": len(us2_players),
        "new_players_vs_us": sorted(us2_players - us_players),
        "unique_bookmakers": sorted(us2_books - us_books),
    }

    out["kalshi_usex"] = _kalshi_market_coverage(payload_usex)
    out["kalshi_explicit"] = _kalshi_market_coverage(payload_kalshi)
    out["kalshi_present_usex"] = any(b.get("key") == "kalshi" for b in (payload_usex or {}).get("bookmakers", []))
    out["kalshi_present_explicit"] = any(b.get("key") == "kalshi" for b in (payload_kalshi or {}).get("bookmakers", []))

    out["two_sided_viability"] = _two_sided_viability(raw_us)
    out["drop_point_trace"] = _drop_point_trace(raw_us)

    # ── Alternative two-sided TD-count markets (player_tds,
    # player_rush_reception_tds) -- ONE additional small request,
    # regions=us only, just these 2 markets. Separate from the 4 calls
    # above so their cost/behavior stays fully isolated and attributable. ──
    status_td_alt, payload_td_alt, err_td_alt = _fetch(event_id, regions="us", markets=TD_ALT_MARKETS)
    if err_td_alt:
        out["errors"].append(f"TD-alt markets (regions=us): {err_td_alt}")

    out["td_alt_breakdown"] = {}
    out["td_alt_raw_sample"] = {}
    out["td_alt_viability"] = {}
    out["td_alt_devig_check"] = {}
    out["td_alt_overlap"] = {}
    for mkt in TD_ALT_MARKETS:
        out["td_alt_breakdown"][mkt] = _ou_half_line_breakdown(payload_td_alt, mkt, FLAG_BOOKS)
        out["td_alt_raw_sample"][mkt] = _ou_raw_sample(payload_td_alt, mkt)
        raw_alt = _ou_raw_rows(payload_td_alt, mkt, event)
        out["td_alt_viability"][mkt] = _ou_two_sided_viability(raw_alt)
        out["td_alt_devig_check"][mkt] = _ou_devig_compat_check(raw_alt, mkt)
        alt_players = {r["player_key"] for r in raw_alt}
        out["td_alt_overlap"][mkt] = {
            "total_players": len(alt_players),
            "overlap_with_anytime_td": sorted(alt_players & us_players),
            "not_in_anytime_td": sorted(alt_players - us_players),
        }

    return out
