# core/lineup_data.py
import os
import requests
import streamlit as st
import pandas as pd

from core.data_sources import fetch_market_lines, get_date_window
from core.pipeline import _consensus_engine
from core.nfl_prop_market_config import normalize_player_key, MIN_BOOKS_FOR_PROP_CONSENSUS

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_SPORT_KEY = "americanfootball_nfl"

POSITION_PROP_MARKETS = {
    # player_anytime_td restored: it's a genuine Yes/No two-outcome market
    # (Odds API market catalog: "Anytime Touchdown Scorer (Yes/No)"), not a
    # numeric-line prop. Previously removed because the old parsing path
    # (get_consensus_prop_line, expects a non-null "line") could never
    # extract a value from it. Now consumed only by
    # get_market_implied_td_fair_probability below, which reuses
    # core/pipeline.py's own devig/consensus engine for this exact
    # two-sided shape (identical to how MARKETS["moneyline"] is handled) --
    # never routed through get_consensus_prop_line's numeric-line parser.
    "QB": [
        "player_pass_yds", "player_pass_tds", "player_pass_interceptions",
        "player_pass_attempts", "player_pass_completions",
        "player_rush_yds", "player_anytime_td",
    ],
    "RB": [
        "player_rush_yds", "player_rush_attempts",
        "player_reception_yds", "player_receptions", "player_anytime_td",
    ],
    "WR": ["player_reception_yds", "player_receptions", "player_anytime_td"],
    "TE": ["player_reception_yds", "player_receptions", "player_anytime_td"],
}

PROP_LABELS = {
    "player_pass_yds": "Passing Yards",
    "player_pass_tds": "Passing TDs",
    "player_pass_interceptions": "Interceptions",
    "player_pass_attempts": "Pass Attempts",
    "player_pass_completions": "Completions",
    "player_rush_yds": "Rushing Yards",
    "player_rush_attempts": "Rush Attempts",
    "player_reception_yds": "Receiving Yards",
    "player_receptions": "Receptions",
}

NFL_TEAMS = {
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

POSITIONS = ["QB", "RB", "WR", "TE", "K", "DST"]
FLEX_POSITIONS = ["RB", "WR", "TE"]


@st.cache_data(ttl=3600, show_spinner=False)
def espn_search_players(query: str) -> list[dict]:
    """
    Search NFL players via ESPN's public site API. Free, unofficial, no key.
    Team info: item['teamRelationships'][0]['core'] (abbreviation, displayName).
    Position not returned here — backfilled via roster lookup.
    Headshot confirmed shape: {'href': <url>, 'alt': <name>}.
    """
    if not query or len(query.strip()) < 2:
        return []
    try:
        r = requests.get(
            "https://site.web.api.espn.com/apis/common/v3/search",
            params={"query": query.strip(), "limit": 10, "type": "player", "sport": "football", "league": "nfl"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        payload = r.json()

        results = []
        for item in payload.get("items", []):
            if item.get("type") != "player":
                continue
            team_rel = (item.get("teamRelationships") or [{}])[0]
            core = team_rel.get("core", {})
            team_abbr = core.get("abbreviation", "")
            team_name = core.get("displayName", "")
            headshot_raw = item.get("headshot")
            headshot_url = headshot_raw.get("href") if isinstance(headshot_raw, dict) else None

            results.append({
                "id": item.get("id"),
                "name": item.get("displayName", ""),
                "team": team_name,
                "team_abbr": team_abbr,
                "position": "",
                "headshot_url": headshot_url,
            })

        for res in results:
            if res["team_abbr"]:
                roster = get_players_by_team(res["team_abbr"])
                match = next(
                    (p for p in roster if p["name"].strip().lower() == res["name"].strip().lower()), None,
                )
                if match:
                    res["position"] = match.get("position", "")
                    if not res["headshot_url"]:
                        res["headshot_url"] = match.get("headshot_url")
        return results
    except Exception:
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_players_by_team(team_abbr: str) -> list[dict]:
    """Raises on any transient failure (network error, non-200 status) so
    st.cache_data never caches that as if it were a genuine roster result
    — a team whose very first request happens to hit a blip would
    otherwise get an empty roster stuck in cache for the full TTL. Only
    a successful response is cached; get_players_by_team below is the
    uncached wrapper that turns a raised failure into the empty-list
    fallback callers expect, on every call, not just the first."""
    r = requests.get(
        f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_abbr}/roster",
        timeout=10,
    )
    r.raise_for_status()
    out = []
    for group in r.json().get("athletes", []):
        for p in group.get("items", []):
            headshot_raw = p.get("headshot")
            headshot_url = headshot_raw.get("href") if isinstance(headshot_raw, dict) else None
            out.append({
                "id": p.get("id"),
                "name": p.get("fullName", ""),
                "position": (p.get("position") or {}).get("abbreviation", ""),
                "team": team_abbr,
                "headshot_url": headshot_url,
            })
    return out


def get_players_by_team(team_abbr: str) -> list[dict]:
    """Full roster for one team — used by Browse Team, and by espn_search_players above."""
    try:
        return _fetch_players_by_team(team_abbr)
    except Exception:
        return []


def get_players_by_position(team_abbr: str, position: str) -> list[dict]:
    roster = get_players_by_team(team_abbr)
    if position == "All":
        return roster
    return [p for p in roster if p.get("position") == position]


def get_team_game_context(supabase, team_name: str, now_utc) -> dict:
    window_start, window_end, sport_keys, _ = get_date_window(now_utc, "Next 7 Days")

    raw, _ = fetch_market_lines(supabase, sport_keys, "spread")
    if raw.empty:
        return {}
    # "side" must be pinned to "home" here: odds_lines carries one row per
    # book per side, and each side's own "line" is already that side's own
    # signed spread (fetch_odds_nfl.py stores outcome.get("point") as-is
    # per side, not a single home-normalized value copied to both rows).
    # Without this filter, .iloc[0] below picks an arbitrary book/side row
    # for this game -- home or away, whichever happened to sort first --
    # and the unconditional negation two lines down (which assumes g["line"]
    # is always the HOME side's spread) would then be wrong exactly when
    # that arbitrary pick was actually the away row.
    games = raw[
        (raw["home_team"].eq(team_name) | raw["away_team"].eq(team_name)) & raw["side"].eq("home")
    ]
    if games.empty:
        return {}
    g = games.sort_values("commence_time").iloc[0]
    is_home = g["home_team"] == team_name
    opponent = g["away_team"] if is_home else g["home_team"]

    total_raw, _ = fetch_market_lines(supabase, sport_keys, "total")
    total_game = total_raw[total_raw["event_id"] == g["event_id"]]
    game_total = None
    if not total_game.empty and "line" in total_game.columns:
        vals = total_game["line"].dropna()
        if not vals.empty:
            game_total = float(vals.iloc[0])

    team_spread = float(g["line"]) if pd.notna(g.get("line")) else None
    if team_spread is not None and not is_home:
        team_spread = -team_spread

    return {
        "event_id": g["event_id"],
        "opponent": opponent,
        "is_home": is_home,
        "commence_time": g["commence_time"],
        "spread": team_spread,
        "game_total": game_total,
        "team_implied_total": calculate_team_implied_total(game_total, team_spread),
    }


def calculate_team_implied_total(game_total: float | None, team_spread: float | None):
    if game_total is None or team_spread is None:
        return None
    return round((game_total / 2) - (team_spread / 2), 1)


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_player_props_for_event(event_id: str, position: str) -> list[dict]:
    markets = POSITION_PROP_MARKETS.get(position, [])
    if not markets or not ODDS_API_KEY:
        return []
    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{ODDS_API_SPORT_KEY}/events/{event_id}/odds",
            params={
                "apiKey": ODDS_API_KEY, "regions": "us",
                "markets": ",".join(markets), "oddsFormat": "american",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        rows = []
        for book in data.get("bookmakers", []):
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    rows.append({
                        "market": market["key"], "player": outcome.get("description", ""),
                        "book": book["key"], "line": outcome.get("point"),
                        "side": outcome.get("name"), "price": outcome.get("price"),
                    })
        return rows
    except Exception:
        return []


def get_consensus_prop_line(prop_rows: list[dict], player_name: str, market_key: str):
    vals = [
        r["line"] for r in prop_rows
        if r["player"].strip().lower() == player_name.strip().lower()
        and r["market"] == market_key and r.get("side") in ("Over", "Yes") and r.get("line") is not None
    ]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return vals[n // 2] if n % 2 else round((vals[n // 2 - 1] + vals[n // 2]) / 2, 1)


def get_relevant_props_for_position(position: str) -> list[str]:
    return POSITION_PROP_MARKETS.get(position, [])


def get_market_implied_td_fair_probability(prop_rows: list[dict], player_name: str) -> float | None:
    """
    Fair (devigged, sportsbook-weighted) probability this player scores
    an anytime touchdown, from The Odds API's player_anytime_td market --
    a genuine two-sided Yes/No market (Odds API market catalog:
    "Anytime Touchdown Scorer (Yes/No)"), structurally identical to
    Moneyline (core/pipeline.py's MARKETS["moneyline"]: two-sided, no
    numeric line). Reuses _consensus_engine (core/pipeline.py, unmodified
    -- imported directly the same way core/nfl_prop_pipeline.py already
    does) for the exact same per-book devig + BOOK_WEIGHTS-weighted
    consensus used everywhere else in this app. No new devig method.

    Only books that post BOTH a Yes and a No price for this player
    contribute -- a book offering only a one-sided "Yes" price is dropped
    entirely by _consensus_engine's own dropna on both implied
    probabilities, the same behavior every other two-sided market in
    this app already relies on. A one-sided price is never treated as a
    real fair probability. Returns None if fewer than
    MIN_BOOKS_FOR_PROP_CONSENSUS books clear that bar for this player,
    matching the same coverage gate already used for the Phase 1
    player-prop pipeline rather than inventing a new threshold.
    """
    target = normalize_player_key(player_name)
    if target is None:
        return None
    rows = []
    for r in prop_rows:
        if r.get("market") != "player_anytime_td":
            continue
        if normalize_player_key(r.get("player")) != target:
            continue
        side = str(r.get("side") or "").strip().lower()
        if side not in ("yes", "no"):
            continue
        price = r.get("price")
        if price is None:
            continue
        rows.append({"book": r.get("book"), "side": side, "price": price})
    if not rows:
        return None

    df = pd.DataFrame(rows)
    df["yes_price"] = df.apply(lambda r: r["price"] if r["side"] == "yes" else None, axis=1)
    df["no_price"] = df.apply(lambda r: r["price"] if r["side"] == "no" else None, axis=1)
    books_df = df.groupby("book", as_index=False).agg(
        yes_price=("yes_price", "max"), no_price=("no_price", "max"),
    )
    books_df["_grp"] = 1
    cons = _consensus_engine(
        books_df, group_keys=["_grp"], side_a_price="yes_price", side_b_price="no_price",
        out_a="yes_fair_prob", out_b="no_fair_prob", label="Anytime TD",
    )
    if cons.empty:
        return None
    row = cons.iloc[0]
    if int(row["num_books"]) < MIN_BOOKS_FOR_PROP_CONSENSUS:
        return None
    return float(row["yes_fair_prob"])


_REC_PTS_BY_SCORING = {"PPR": 1.0, "Half PPR": 0.5, "Standard": 0.0}

# Standard fantasy-scoring weights per stat -- matching the same
# convention nflreadpy's own fantasy_points/fantasy_points_ppr fields use
# (4pt passing TDs, 0.04 pt/passing yard, 0.1 pt/rush-or-receiving yard,
# 6pt rush/receiving TD, -2pt interception; core/nfl_defense_data.py::
# _fantasy_points_for reads those precomputed nflreadpy totals directly
# for historical stats). This repo has no existing named constant for the
# per-stat weights themselves, only the precomputed totals, so these are
# newly defined here -- intentionally matching that same convention
# rather than inventing different weights. No 4pt-vs-6pt passing-TD
# league toggle exists anywhere in this app (Scoring is PPR/Half PPR/
# Standard only), so this follows nflreadpy's own standard-scoring
# convention (4pt) rather than assuming a 6pt variant.
_PTS_PER_PASS_YARD = 0.04
_PTS_PER_PASS_TD = 4.0
_PTS_PER_INTERCEPTION = -2.0
_PTS_PER_RUSH_YARD = 0.1
_PTS_PER_REC_YARD = 0.1
_PTS_PER_TD = 6.0  # rushing/receiving TD, and the market-implied "any TD" proxy


def calculate_market_implied_fantasy_points(
    props: dict, position: str, scoring: str, td_fair_prob: float | None,
) -> float | None:
    """
    V1 market-implied fantasy-points estimate: converts the existing
    weighted-consensus prop lines (props, from get_consensus_prop_line --
    the same devigged consensus already used for the Player Props table,
    not recalculated differently here) plus Market-Implied TD Fair
    Probability into a fantasy-scoring estimate via the standard weights
    above. This is a V1 point estimate, not a statistically exact
    expectation -- a sportsbook Over/Under line is not guaranteed to
    equal the true distribution mean.

    Returns None ("Insufficient market data") rather than treating a
    missing REQUIRED component as zero. Per position, minimum required:
      QB: passing yards AND passing TDs
      RB: rushing yards
      WR/TE: receiving yards AND receptions
    Interceptions/rushing yards (QB) and touchdown probability (all
    positions) are additive-only -- they simply don't contribute if
    unavailable, since they're explicitly optional rather than a hard
    requirement (matching how the actual market coverage varies by
    player/book).
    """
    rec_pts = _REC_PTS_BY_SCORING.get(scoring, 0.0)
    td_component = (td_fair_prob * _PTS_PER_TD) if td_fair_prob is not None else 0.0

    if position == "QB":
        pass_yds = props.get("player_pass_yds")
        pass_tds = props.get("player_pass_tds")
        if pass_yds is None or pass_tds is None:
            return None
        total = pass_yds * _PTS_PER_PASS_YARD + pass_tds * _PTS_PER_PASS_TD
        ints = props.get("player_pass_interceptions")
        if ints is not None:
            total += ints * _PTS_PER_INTERCEPTION
        rush_yds = props.get("player_rush_yds")
        if rush_yds is not None:
            total += rush_yds * _PTS_PER_RUSH_YARD
        total += td_component
        return round(total, 1)

    if position == "RB":
        rush_yds = props.get("player_rush_yds")
        if rush_yds is None:
            return None
        total = rush_yds * _PTS_PER_RUSH_YARD
        rec_yds = props.get("player_reception_yds")
        if rec_yds is not None:
            total += rec_yds * _PTS_PER_REC_YARD
        recs = props.get("player_receptions")
        if recs is not None:
            total += recs * rec_pts
        total += td_component
        return round(total, 1)

    if position in ("WR", "TE"):
        rec_yds = props.get("player_reception_yds")
        recs = props.get("player_receptions")
        if rec_yds is None or recs is None:
            return None
        total = rec_yds * _PTS_PER_REC_YARD + recs * rec_pts
        total += td_component
        return round(total, 1)

    return None


def build_player_comparison(supabase, players: list[dict], now_utc, scoring: str = "PPR") -> list[dict]:
    enriched = []
    for p in players:
        ctx = get_team_game_context(supabase, p["team"], now_utc) if p.get("team") else {}
        props = {}
        td_fair_prob = None
        if ctx.get("event_id"):
            raw_props = fetch_player_props_for_event(ctx["event_id"], p["position"])
            for market_key in get_relevant_props_for_position(p["position"]):
                if market_key == "player_anytime_td":
                    # Yes/No market -- handled via
                    # get_market_implied_td_fair_probability below, never
                    # routed through get_consensus_prop_line's numeric-line
                    # parser (which would only ever find no line for it).
                    continue
                props[market_key] = get_consensus_prop_line(raw_props, p["name"], market_key)
            td_fair_prob = get_market_implied_td_fair_probability(raw_props, p["name"])
        market_implied_fp = calculate_market_implied_fantasy_points(props, p["position"], scoring, td_fair_prob)
        enriched.append({
            **p, "context": ctx, "props": props,
            "td_fair_prob": td_fair_prob, "market_implied_fantasy_points": market_implied_fp,
        })
    return enriched


def generate_key_differences(enriched_players: list[dict]) -> list[str]:
    notes = []
    totals = [(p["name"], p["context"].get("team_implied_total")) for p in enriched_players if p["context"].get("team_implied_total") is not None]
    if len(totals) >= 2:
        totals.sort(key=lambda x: x[1], reverse=True)
        diff = round(totals[0][1] - totals[-1][1], 1)
        notes.append(f"{totals[0][0]}'s team is implied to score {diff} more points.")

    for market_key, label in [("player_reception_yds", "receiving yards"), ("player_rush_yds", "rushing yards")]:
        vals = [(p["name"], p["props"].get(market_key)) for p in enriched_players if p["props"].get(market_key) is not None]
        if len(vals) >= 2:
            vals.sort(key=lambda x: x[1], reverse=True)
            notes.append(f"{vals[0][0]} has the higher {label} line ({vals[0][1]} vs {vals[-1][1]}).")

    td_vals = [(p["name"], p["props"].get("player_anytime_td")) for p in enriched_players if p["props"].get("player_anytime_td") is not None]
    if len(td_vals) >= 2:
        td_vals.sort(key=lambda x: x[1])
        notes.append(f"{td_vals[0][0]} has the more favorable anytime-TD price.")

    if not notes:
        notes.append("Not enough market data available yet to compare these players — check back closer to kickoff as more books post lines.")
    return notes[:4]
