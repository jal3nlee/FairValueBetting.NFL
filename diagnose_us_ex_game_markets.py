# diagnose_us_ex_game_markets.py
# Read-only, standalone live investigation of The Odds API's NFL GAME-MARKET
# (h2h/spreads/totals) coverage under regions=us_ex, for Kalshi, Polymarket,
# Novig, ProphetX, and BetOpenly. Uses the exact SAME bulk endpoint
# fetch_odds_nfl.py itself uses (/v4/sports/{sport}/odds), just with
# regions=us_ex instead of "us". Diagnostic only -- NOT imported by the app,
# NOT part of ingestion, makes NO Supabase writes, NO model/weight changes.
# Kept in the repo (like fetch_odds_nfl.py/fetch_odds_nfl_props.py) as a
# standalone manual-run script, not wired into anything.
# Only needs ODDS_API_KEY -- no Supabase credentials required, since this
# queries the bulk endpoint directly rather than reading already-ingested
# events from Supabase.
#
# Cost: bulk-odds endpoint cost = markets specified x regions =
# 3 markets x 1 region = 3 credits per run (two requests total, including
# the regions=us comparison call below, so ~6 credits per run).
#
# Run anywhere ODDS_API_KEY is set (Render shell or otherwise):
#   python3 diagnose_us_ex_game_markets.py
import json
import os

import requests

ODDS_API_KEY = os.environ["ODDS_API_KEY"]
EXCHANGE_KEYS = ["kalshi", "polymarket", "novig", "prophetx", "betopenly"]
MARKETS = ["h2h", "spreads", "totals"]

url = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
params = {
    "apiKey": ODDS_API_KEY,
    "regions": "us_ex",
    "markets": ",".join(MARKETS),
    "oddsFormat": "american",
}
resp = requests.get(url, params=params, timeout=20)
print(f"=== regions=us_ex, markets={MARKETS} ===  status={resp.status_code}")
print(f"Remaining credits header: {resp.headers.get('x-requests-remaining')}  "
      f"Used this call: {resp.headers.get('x-requests-last')}\n")

if resp.status_code != 200:
    print(f"Error body: {resp.text[:800]}")
    raise SystemExit(1)

games = resp.json()
print(f"Total NFL games returned: {len(games)}")
if games:
    print(f"Sample game: {games[0].get('away_team')} @ {games[0].get('home_team')}  "
          f"commence_time={games[0].get('commence_time')}\n")

# Also fetch regions=us for the SAME games, for an event-coverage comparison.
resp_us = requests.get(url, params={**params, "regions": "us"}, timeout=20)
games_us = resp_us.json() if resp_us.status_code == 200 else []
print(f"For comparison -- regions=us total games returned: {len(games_us)}\n")

# ═══════════════════════════════════════════════════════════════════
# 1. Live NFL coverage per exchange
# ═══════════════════════════════════════════════════════════════════
print("=" * 78)
print("1. LIVE NFL COVERAGE PER EXCHANGE (regions=us_ex)")
print("=" * 78)
per_exchange = {k: {"games_with_book": 0, "markets_seen": set(), "sample_outcomes": {}} for k in EXCHANGE_KEYS}
all_books_seen = set()

for game in games:
    for book in game.get("bookmakers", []):
        all_books_seen.add(book.get("key"))
        bk = book.get("key")
        if bk not in EXCHANGE_KEYS:
            continue
        per_exchange[bk]["games_with_book"] += 1
        for m in book.get("markets", []):
            mkey = m.get("key")
            per_exchange[bk]["markets_seen"].add(mkey)
            if mkey not in per_exchange[bk]["sample_outcomes"]:
                per_exchange[bk]["sample_outcomes"][mkey] = {
                    "game": f"{game.get('away_team')} @ {game.get('home_team')}",
                    "outcomes": m.get("outcomes", []),
                    "last_update": m.get("last_update"),
                }

print(f"\nAll bookmaker keys seen under regions=us_ex (any market): {sorted(all_books_seen)}\n")

for bk in EXCHANGE_KEYS:
    info = per_exchange[bk]
    pct = (100 * info["games_with_book"] / len(games)) if games else 0
    print(f"  {bk:12s} present in {info['games_with_book']}/{len(games)} games ({pct:.0f}%)  "
          f"markets returned: {sorted(info['markets_seen']) or 'NONE'}")

# ═══════════════════════════════════════════════════════════════════
# 2. Representative raw responses -- Kalshi and Polymarket
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("2. REPRESENTATIVE RAW RESPONSES")
print("=" * 78)
for bk in ["kalshi", "polymarket"]:
    info = per_exchange[bk]
    print(f"\n--- {bk} ---")
    if not info["sample_outcomes"]:
        print("  NO DATA -- this bookmaker did not appear in the regions=us_ex response at all.")
        continue
    for mkey, sample in info["sample_outcomes"].items():
        print(f"  Market: {mkey}  (game: {sample['game']})  last_update: {sample['last_update']}")
        for o in sample["outcomes"]:
            print(f"    raw outcome: {json.dumps(o)}")
        # Print the FULL market dict too (in case there are fields beyond
        # key/outcomes/last_update that hint at price semantics).
        print(f"    full market object keys: {list(sample.get('outcomes', [{}])[0].keys()) if sample['outcomes'] else '[]'}")

# ═══════════════════════════════════════════════════════════════════
# 3. What does `price` represent? -- look for any bid/ask/size/liquidity
#    fields, and compare implied-probability sums vs. traditional books.
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("3. PRICE SEMANTICS -- field inspection + implied-prob-sum comparison")
print("=" * 78)


def implied_prob(american):
    if american is None:
        return None
    a = int(american)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


all_outcome_fields = set()
for bk in EXCHANGE_KEYS:
    for sample in per_exchange[bk]["sample_outcomes"].values():
        for o in sample["outcomes"]:
            all_outcome_fields |= set(o.keys())
print(f"All distinct fields observed across ALL exchange outcome objects: {sorted(all_outcome_fields)}")
print("  (fetch_odds_nfl.py currently only reads 'name', 'price', 'point' from each outcome --")
print("   any field NOT in that set above would currently be silently ignored/unused.)")

print("\nImplied-probability-sum comparison (h2h market), exchange vs. traditional book, same game:")
if games:
    g0 = games[0]
    for book in g0.get("bookmakers", []):
        bk = book.get("key")
        if bk not in EXCHANGE_KEYS and bk not in ("draftkings", "fanduel", "pinnacle", "eu_pinnacle"):
            continue
        for m in book.get("markets", []):
            if m.get("key") != "h2h":
                continue
            probs = [implied_prob(o.get("price")) for o in m.get("outcomes", [])]
            probs = [p for p in probs if p is not None]
            if len(probs) == 2:
                print(f"  {bk:12s} h2h implied-prob sum = {sum(probs):.4f}  "
                      f"(1.0 = no margin either way; >1 = house-side margin embedded, "
                      f"exactly like a sportsbook's vig)")
else:
    print("  No games available to compare.")

# ═══════════════════════════════════════════════════════════════════
# 4. Two-sided market behavior -- does the response look like a normal
#    sportsbook moneyline/spread/total, and would fetch_odds_nfl.py's
#    OWN existing side-detection logic classify it correctly?
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("4. TWO-SIDED MARKET BEHAVIOR + fetch_odds_nfl.py PARSE-COMPATIBILITY TEST")
print("=" * 78)
print("  (Replicates fetch_odds_nfl.py's EXACT existing side-detection logic, unmodified,")
print("   against the live exchange response -- does not change that file.)")

for bk in EXCHANGE_KEYS:
    info = per_exchange[bk]
    if not info["sample_outcomes"]:
        continue
    for mkey, sample in info["sample_outcomes"].items():
        game = next((g for g in games if f"{g.get('away_team')} @ {g.get('home_team')}" == sample["game"]), None)
        if game is None:
            continue
        home_team, away_team = game.get("home_team"), game.get("away_team")
        sides_detected = []
        for o in sample["outcomes"]:
            name = o.get("name")
            if name in ("Over", "Under"):
                side = name.lower()
            elif name == home_team:
                side = "home"
            elif name == away_team:
                side = "away"
            else:
                side = None  # <-- exactly what fetch_odds_nfl.py stores; None rows get
                             #     silently dropped later by core/pipeline.py's valid_sides filter
            sides_detected.append((name, side))
        both_sides_present = len(sample["outcomes"]) == 2
        any_unparsed = any(s is None for _, s in sides_detected)
        print(f"  {bk:12s} / {mkey:10s}: outcomes={sides_detected}  "
              f"both_sides_present={both_sides_present}  "
              f"{'*** UNPARSEABLE SIDE (would be silently dropped) ***' if any_unparsed else 'parses cleanly with existing logic'}")

print("\nDone. (Read-only -- no Supabase writes, no ingestion code touched, no model changes.)")
