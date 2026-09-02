# core/nfl_prop_market_config.py
# Phase 1 NFL player-prop market definitions. Reuses core/pipeline.py's
# existing MarketConfig dataclass unmodified — its fields are already
# generic over side_a/side_b naming (Total already uses "over"/"under"),
# so a prop market is just another MarketConfig instance, not a new
# concept. This file adds no new math — only market metadata, player-
# name normalization, and the Phase 1 coverage/scope constants.
import re

from core.pipeline import MarketConfig

# ── Phase 1 approved markets only ──────────────────────────────────
# Anytime/1st/Last TD, alternates, combo props, defense/kicking, and
# live/in-game props are deliberately excluded from Phase 1 (see the
# player-prop audit) — every market below is a normal two-sided
# Over/Under, matching the exact math this pipeline already assumes.
PROP_MARKETS: dict[str, MarketConfig] = {
    "player_pass_yds": MarketConfig(
        name="Passing Yards", db_market_key="player_pass_yds", odds_api_market="player_pass_yds",
        valid_sides=("over", "under"), side_a="over", side_b="under",
        price_a_col="over_price", price_b_col="under_price",
        fair_a_col="over_fair_prob", fair_b_col="under_fair_prob",
        label_a="Over", label_b="Under", group_keys=("player_key", "line"), line_col="line",
        display_line=True, market_label="Passing Yards",
    ),
    "player_pass_tds": MarketConfig(
        name="Passing TDs", db_market_key="player_pass_tds", odds_api_market="player_pass_tds",
        valid_sides=("over", "under"), side_a="over", side_b="under",
        price_a_col="over_price", price_b_col="under_price",
        fair_a_col="over_fair_prob", fair_b_col="under_fair_prob",
        label_a="Over", label_b="Under", group_keys=("player_key", "line"), line_col="line",
        display_line=True, market_label="Passing TDs",
    ),
    "player_rush_yds": MarketConfig(
        name="Rushing Yards", db_market_key="player_rush_yds", odds_api_market="player_rush_yds",
        valid_sides=("over", "under"), side_a="over", side_b="under",
        price_a_col="over_price", price_b_col="under_price",
        fair_a_col="over_fair_prob", fair_b_col="under_fair_prob",
        label_a="Over", label_b="Under", group_keys=("player_key", "line"), line_col="line",
        display_line=True, market_label="Rushing Yards",
    ),
    "player_reception_yds": MarketConfig(
        name="Receiving Yards", db_market_key="player_reception_yds", odds_api_market="player_reception_yds",
        valid_sides=("over", "under"), side_a="over", side_b="under",
        price_a_col="over_price", price_b_col="under_price",
        fair_a_col="over_fair_prob", fair_b_col="under_fair_prob",
        label_a="Over", label_b="Under", group_keys=("player_key", "line"), line_col="line",
        display_line=True, market_label="Receiving Yards",
    ),
    "player_receptions": MarketConfig(
        name="Receptions", db_market_key="player_receptions", odds_api_market="player_receptions",
        valid_sides=("over", "under"), side_a="over", side_b="under",
        price_a_col="over_price", price_b_col="under_price",
        fair_a_col="over_fair_prob", fair_b_col="under_fair_prob",
        label_a="Over", label_b="Under", group_keys=("player_key", "line"), line_col="line",
        display_line=True, market_label="Receptions",
    ),
}

# Explicitly NOT in PROP_MARKETS (documented, not just omitted, so it's
# clear these were considered and deliberately deferred, not forgotten):
#   player_anytime_td, player_1st_td, player_last_td  — Yes/No structure,
#     not a two-sided Over/Under price pair; needs separate treatment.
#   *_alternate markets, combo props, defense/kicking — deferred per
#     Phase 1 scope.

# ── Sportsbook-coverage gate ────────────────────────────────────────
# "Do not display a Fair Price as if it represents meaningful consensus
# when only one sportsbook contributes a usable two-sided market." This
# reuses the existing mi_num_books field build_market_intelligence
# already computes (unmodified) — Phase 1 simply requires it to be >= 2
# for a prop group to be included at all, rather than inventing a new
# confidence methodology. consensus_rating()'s existing "Little" label
# (core/pipeline.py) still applies for display among the groups that
# pass this gate; this constant controls inclusion, not the label text.
MIN_BOOKS_FOR_PROP_CONSENSUS = 2

# ── Player-name normalization ───────────────────────────────────────
# The Odds API supplies player identity only as a free-text outcome
# description (core/lineup_data.py::fetch_player_props_for_event already
# reads this as outcome.get("description")) — no stable player ID.
# Mirrors core/nflverse_data.py::_norm_name's exact suffix list for
# consistency with the one normalization convention already established
# in this codebase, extended with punctuation/whitespace handling since
# sportsbook name strings are less clean than nflreadpy's own names.
_SUFFIXES = (" jr.", " jr", " sr.", " sr", " ii", " iii", " iv")
_PUNCT_RE = re.compile(r"[.\-'’]")
_WS_RE = re.compile(r"\s+")


def normalize_player_key(raw_name: str | None) -> str | None:
    """
    Returns a normalized player key for grouping (lowercase, punctuation
    stripped, suffixes removed, whitespace collapsed), or None if the
    input is empty/unusable. None means "exclude this row" to the
    caller — normalization never guesses at an ambiguous or missing
    name; it only cleans up unambiguous formatting variation
    (capitalization, punctuation, suffixes, whitespace).
    """
    if not raw_name or not str(raw_name).strip():
        return None
    n = _PUNCT_RE.sub("", str(raw_name).lower())
    n = _WS_RE.sub(" ", n).strip()
    for suffix in _SUFFIXES:
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
            break
    return n or None
