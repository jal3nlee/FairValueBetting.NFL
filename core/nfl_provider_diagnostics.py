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

from core.pipeline import _consensus_engine
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

# One combined request per scenario in production would cost
# (unique markets returned) x (regions requested); this diagnostic makes
# 4 separate single-scope requests instead (to isolate what each region
# contributes on its own), each up to len(ALL_MARKETS)=7 credits under
# the event-odds endpoint's documented "markets returned x regions"
# formula -- worst case ~28 credits total, one-time per click (cached
# afterward, see tabs/fantasy_rankings.py).
ESTIMATED_MAX_CREDITS = 28


def _fetch(event_id: str, regions: str = None, bookmakers: str = None):
    """One read-only GET to the Odds API event-odds endpoint. Returns
    (status_code, payload_or_None, error_message_or_None). Never raises;
    a network failure is reported back as a clean message, not a
    traceback, and never includes ODDS_API_KEY's value."""
    if not ODDS_API_KEY:
        return None, None, "ODDS_API_KEY is not configured in this environment."
    url = f"https://api.the-odds-api.com/v4/sports/{ODDS_API_SPORT_KEY}/events/{event_id}/odds"
    params = {"apiKey": ODDS_API_KEY, "markets": ",".join(ALL_MARKETS), "oddsFormat": "american"}
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
    Rankings already reads), then makes exactly 4 live Odds API requests
    (regions=us, regions=us2, regions=us_ex, bookmakers=kalshi), each for
    all 7 current prop markets, and returns a dict of small DataFrames/
    values ready for display -- never raw JSON, never a secret value.
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

    return out
