# fetch_odds_nfl_props.py
# Standalone NFL player-prop ingestion — Phase 1. Deliberately a SEPARATE
# script from fetch_odds_nfl.py (own tables, own workflow, own
# concurrency group) so the existing, proven game-market ingestion path
# is never at risk from anything in here. Only reads (never writes)
# odds_lines, to reuse the schedule fetch_odds_nfl.py already maintains
# instead of adding a second NFL-schedule dependency.
#
# Flow: identify upcoming NFL events (read-only, from the existing
# odds_lines table) -> for each event due for a refresh under its own
# tiered pregame cadence -> request the Phase 1 prop markets via The
# Odds API's event-specific endpoint -> normalize player/market/line/
# side/price/book -> write snapshot+line rows to the dedicated prop
# tables -> log API-usage metadata for real-usage measurement.
import os
import time
import uuid
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

from core.odds_math import parse_iso_dt_utc
from core.nfl_prop_market_config import PROP_MARKETS, normalize_player_key

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ODDS_API_SPORT_KEY = "americanfootball_nfl"
SPORT = "NFL"
PHASE1_MARKET_KEYS = list(PROP_MARKETS.keys())  # player_pass_yds, player_pass_tds, player_rush_yds,
                                                 # player_reception_yds, player_receptions

# ── Retry/backoff (same shape as fetch_odds_nfl.py's; duplicated rather
# than shared, matching that script's existing standalone convention). ──
REQUEST_TIMEOUT = 15
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# ── Tiered pregame cadence — "very slow when kickoff is far away,
# increasing frequency as kickoff approaches, ~5min close to kickoff,
# no pregame polling after kickoff." Deliberately more conservative
# than fetch_odds_nfl.py's cadence at the far end, since a prop pull
# costs credits per market RETURNED for each event individually,
# unlike the flat-cost bulk game-market request. ──
FAR_OUT_INTERVAL_SECONDS = 3600     # > 2h to kickoff: hourly
APPROACHING_INTERVAL_SECONDS = 900  # 30min-2h to kickoff: every 15 min
FINAL_INTERVAL_SECONDS = 300        # < 30min to kickoff: every 5 min
APPROACHING_WINDOW = timedelta(hours=2)
FINAL_WINDOW = timedelta(minutes=30)

# How far ahead to consider an event "upcoming" for prop polling at all
# — props are typically not posted much earlier than this by books, and
# this bounds how many events get queried per run.
SCHEDULE_LOOKAHEAD = timedelta(days=6)


def _cadence_seconds(now_utc: datetime, commence_utc: datetime):
    """Returns (interval_seconds, state) for one event, or (None, 'kickoff-passed')
    if this event should no longer be polled for pregame props at all."""
    if now_utc >= commence_utc:
        return None, "kickoff-passed"
    until_kickoff = commence_utc - now_utc
    if until_kickoff <= FINAL_WINDOW:
        return FINAL_INTERVAL_SECONDS, "final-approach"
    if until_kickoff <= APPROACHING_WINDOW:
        return APPROACHING_INTERVAL_SECONDS, "approaching"
    return FAR_OUT_INTERVAL_SECONDS, "far-out"


def _get_upcoming_events(supabase):
    """
    Read-only: distinct upcoming NFL events, sourced from the existing
    odds_lines table (already kept fresh by fetch_odds_nfl.py) rather
    than adding a second schedule dependency. Never writes to odds_lines.
    """
    now_utc = datetime.now(timezone.utc)
    horizon = now_utc + SCHEDULE_LOOKAHEAD
    try:
        res = (
            supabase.table("odds_lines")
            .select("event_id,home_team,away_team,commence_time")
            .eq("sport", SPORT)
            .gte("commence_time", now_utc.isoformat())
            .lte("commence_time", horizon.isoformat())
            .limit(2000)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        print(f"Could not read upcoming events from odds_lines (skipping this run): {type(e).__name__}: {e}")
        return []

    events = {}
    for r in rows:
        eid = r.get("event_id")
        ct = parse_iso_dt_utc(r.get("commence_time"))
        if not eid or not ct:
            continue
        events[eid] = {
            "event_id": eid, "home_team": r.get("home_team"),
            "away_team": r.get("away_team"), "commence_time": ct,
        }
    return list(events.values())


def _get_last_prop_pull_time(supabase, event_id: str):
    """Most recent player_prop_snapshots.pulled_at for this event, across
    any market — the per-event throttle reference. None if never pulled."""
    try:
        res = (
            supabase.table("player_prop_snapshots")
            .select("pulled_at")
            .eq("event_id", event_id)
            .order("pulled_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        return parse_iso_dt_utc(rows[0].get("pulled_at"))
    except Exception as e:
        print(f"Could not determine last prop pull time for {event_id} (defaulting to fetch now): "
              f"{type(e).__name__}: {e}")
        return None


def _parse_retry_after(resp):
    raw = resp.headers.get("Retry-After") if resp is not None else None
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, BACKOFF_MAX_SECONDS)


def _fetch_event_odds_with_retries(url: str, params: dict, label: str):
    last_resp = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            print(f"Request error for {label} (attempt {attempt}/{MAX_ATTEMPTS}): {type(e).__name__}: {e}")
            if attempt == MAX_ATTEMPTS:
                print(f"Giving up on {label} after {MAX_ATTEMPTS} attempts — skipping this event.")
                return None
            time.sleep(min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS))
            continue

        last_resp = resp
        if resp.status_code == 200:
            return resp
        if resp.status_code not in RETRYABLE_STATUS_CODES:
            return resp
        if attempt == MAX_ATTEMPTS:
            print(f"Giving up on {label} after {MAX_ATTEMPTS} attempts — last status {resp.status_code}: {resp.text}")
            return None
        retry_after = _parse_retry_after(resp)
        delay = retry_after if retry_after is not None else min(
            BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS
        )
        print(f"Transient error for {label} (attempt {attempt}/{MAX_ATTEMPTS}): "
              f"status {resp.status_code} — retrying in {delay:.1f}s")
        time.sleep(delay)
    return last_resp


def _normalize_and_write(event: dict, api_payload: dict):
    """
    Writes one player_prop_snapshots + player_prop_lines batch PER
    market actually present in the response (mirrors odds_snapshots'
    convention of one snapshot per market) — only for markets that
    returned data, matching the Odds API's own "cost = markets
    returned" accounting. Rows whose player name can't be safely
    normalized (normalize_player_key returns None) are skipped rather
    than written with an ambiguous identity.
    Returns (markets_written, lines_written) for usage logging.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    by_market: dict[str, list] = {}
    for book in api_payload.get("bookmakers", []):
        book_key = book.get("key")
        for market in book.get("markets", []):
            market_key = market.get("key")
            if market_key not in PROP_MARKETS:
                continue
            for outcome in market.get("outcomes", []):
                side_raw = (outcome.get("name") or "").strip().lower()
                if side_raw not in ("over", "under"):
                    continue  # Phase 1 markets are strictly two-sided
                player_display = outcome.get("description")
                player_key = normalize_player_key(player_display)
                if player_key is None:
                    continue  # unusable/ambiguous name — exclude, don't guess
                by_market.setdefault(market_key, []).append({
                    "event_id": event["event_id"],
                    "home_team": event["home_team"],
                    "away_team": event["away_team"],
                    "commence_time": event["commence_time"].isoformat(),
                    "book": book_key,
                    "market": market_key,
                    "side": side_raw,
                    "line": outcome.get("point"),
                    "price": outcome.get("price"),
                    "player_key": player_key,
                    "player_display": player_display,
                })

    markets_written, lines_written = 0, 0
    for market_key, lines in by_market.items():
        if not lines:
            continue
        snapshot_id = str(uuid.uuid4())
        supabase.table("player_prop_snapshots").insert({
            "id": snapshot_id,
            "event_id": event["event_id"],
            "market": market_key,
            "pulled_at": now_iso,
            "home_team": event["home_team"],
            "away_team": event["away_team"],
            "commence_time": event["commence_time"].isoformat(),
        }).execute()
        for line in lines:
            line["snapshot_id"] = snapshot_id
        supabase.table("player_prop_lines").insert(lines).execute()
        markets_written += 1
        lines_written += len(lines)
    return markets_written, lines_written


def _log_usage(event_id: str, markets_requested: int, markets_returned: int):
    """
    Best-effort API-usage instrumentation — a failure here must never
    block ingestion. estimated_credits follows The Odds API's documented
    formula (markets returned x regions); regions=1 here (region="us").
    """
    try:
        supabase.table("player_prop_fetch_log").insert({
            "id": str(uuid.uuid4()),
            "run_at": datetime.now(timezone.utc).isoformat(),
            "event_id": event_id,
            "markets_requested": markets_requested,
            "markets_returned": markets_returned,
            "estimated_credits": markets_returned * 1,
        }).execute()
    except Exception as e:
        print(f"Could not write usage log for {event_id} (non-fatal): {type(e).__name__}: {e}")


def run_pull():
    now_utc = datetime.now(timezone.utc)
    events = _get_upcoming_events(supabase)
    if not events:
        print("No upcoming NFL events found in odds_lines — nothing to poll.")
        return

    total_events_polled = 0
    total_markets_written = 0
    total_lines_written = 0
    total_estimated_credits = 0

    for event in events:
        interval_seconds, state = _cadence_seconds(now_utc, event["commence_time"])
        if interval_seconds is None:
            continue  # kickoff has passed — no pregame prop polling in Phase 1

        last_pulled = _get_last_prop_pull_time(supabase, event["event_id"])
        if last_pulled is not None:
            elapsed = (now_utc - last_pulled).total_seconds()
            if elapsed < interval_seconds:
                continue  # not due yet under this event's own cadence tier

        print(f"Polling props for event {event['event_id']} "
              f"({event['away_team']} @ {event['home_team']}) — cadence state: {state}")
        url = f"https://api.the-odds-api.com/v4/sports/{ODDS_API_SPORT_KEY}/events/{event['event_id']}/odds"
        params = {
            "apiKey": ODDS_API_KEY, "regions": "us",
            "markets": ",".join(PHASE1_MARKET_KEYS), "oddsFormat": "american",
        }
        resp = _fetch_event_odds_with_retries(url, params, label=event["event_id"])
        total_events_polled += 1
        if resp is None or resp.status_code != 200:
            if resp is not None:
                print(f"Error for event {event['event_id']}: {resp.status_code} {resp.text}")
            _log_usage(event["event_id"], len(PHASE1_MARKET_KEYS), 0)
            continue

        payload = resp.json()
        markets_written, lines_written = _normalize_and_write(event, payload)
        total_markets_written += markets_written
        total_lines_written += lines_written
        total_estimated_credits += markets_written  # 1 region
        _log_usage(event["event_id"], len(PHASE1_MARKET_KEYS), markets_written)

    print(
        f"Done. Events polled: {total_events_polled}, market snapshots written: {total_markets_written}, "
        f"lines written: {total_lines_written}, estimated credits this run: {total_estimated_credits}."
    )


if __name__ == "__main__":
    if not ODDS_API_KEY or not SUPABASE_URL or not SUPABASE_KEY:
        print("Missing ODDS_API_KEY, SUPABASE_URL, or SUPABASE_KEY environment variables.")
    else:
        run_pull()
