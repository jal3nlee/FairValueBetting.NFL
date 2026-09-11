# core/nflverse_data.py
import streamlit as st

from core.data_sources import infer_current_week_index

try:
    import nflreadpy as nfl
    _NFLVERSE_AVAILABLE = True
except Exception:
    _NFLVERSE_AVAILABLE = False

RECENCY_DECAY = 0.85

POSITION_METRICS = {
    "WR": ["targets_per_game", "target_share", "receiving_yards_per_game"],
    "TE": ["targets_per_game", "target_share", "receiving_yards_per_game"],
    "RB": ["carries_per_game", "carry_share", "targets_per_game"],
    "QB": ["attempts_per_game", "rush_attempts_per_game", "passing_yards_per_game"],
}

METRIC_LABELS = {
    "targets_per_game":        "Targets / Game",
    "target_share":            "Target Share",
    "receiving_yards_per_game":"Receiving Yards / Game",
    "carries_per_game":        "Carries / Game",
    "carry_share":             "Carry Share",
    "attempts_per_game":       "Pass Attempts / Game",
    "rush_attempts_per_game":  "Rush Attempts / Game",
    "passing_yards_per_game":  "Passing Yards / Game",
    "receptions_per_game":     "Receptions / Game",
    "rush_yards_per_game":     "Rushing Yards / Game",
    "passing_tds_per_game":    "Passing TDs / Game",
    "completions_per_game":    "Completions / Game",
}
PERCENT_METRICS = {"target_share", "carry_share"}

LINEUP_USAGE_METRICS = {
    "WR": ["targets_per_game", "target_share", "receptions_per_game", "receiving_yards_per_game"],
    "TE": ["targets_per_game", "target_share", "receptions_per_game", "receiving_yards_per_game"],
    "RB": ["carries_per_game", "carry_share", "targets_per_game", "receptions_per_game", "rush_yards_per_game"],
    "QB": ["attempts_per_game", "passing_yards_per_game", "rush_attempts_per_game", "rush_yards_per_game"],
}

CARD_SEASON_METRICS = {
    "QB": ["passing_yards_per_game", "passing_tds_per_game", "rush_yards_per_game", "interceptions_per_game"],
    "RB": ["carries_per_game", "rush_yards_per_game", "targets_per_game", "total_tds_per_game"],
    "WR": ["targets_per_game", "receptions_per_game", "receiving_yards_per_game", "receiving_tds_per_game"],
    "TE": ["targets_per_game", "receptions_per_game", "receiving_yards_per_game", "receiving_tds_per_game"],
}
CARD_METRIC_LABELS = {
    "passing_yards_per_game":   "Pass Yds/G",
    "passing_tds_per_game":     "Pass TD/G",
    "rush_yards_per_game":      "Rush Yds/G",
    "interceptions_per_game":   "INT/G",
    "carries_per_game":         "Car/G",
    "targets_per_game":         "Tgt/G",
    "receptions_per_game":      "Rec/G",
    "receiving_yards_per_game": "Rec Yds/G",
    "receiving_tds_per_game":   "TD/G",
    "total_tds_per_game":       "TD/G",
}

EXPANDED_SEASON_METRICS = {
    "QB": ["attempts_per_game", "completions_per_game", "passing_yards_per_game",
           "passing_tds_per_game", "interceptions_per_game", "rush_attempts_per_game", "rush_yards_per_game"],
    "RB": ["carries_per_game", "rush_yards_per_game", "total_tds_per_game",
           "targets_per_game", "receptions_per_game", "receiving_yards_per_game"],
    "WR": ["targets_per_game", "target_share", "receptions_per_game",
           "receiving_yards_per_game", "receiving_tds_per_game"],
    "TE": ["targets_per_game", "target_share", "receptions_per_game",
           "receiving_yards_per_game", "receiving_tds_per_game"],
}

_METRIC_FIELD = {
    "targets_per_game": "targets",
    "target_share": "target_share",
    "receiving_yards_per_game": "receiving_yards",
    "receptions_per_game": "receptions",
    "carries_per_game": "carries",
    "carry_share": "_carry_share",
    "rush_yards_per_game": "rushing_yards",
    "attempts_per_game": "attempts",
    "passing_yards_per_game": "passing_yards",
    "rush_attempts_per_game": "carries",
    "passing_tds_per_game": "passing_tds",
    "interceptions_per_game": "passing_interceptions",
    "receiving_tds_per_game": "receiving_tds",
    "total_tds_per_game": "_total_td",
    "completions_per_game": "completions",
}

_BLEND_SCHEDULE = {0: 0.40, 1: 0.55, 2: 0.70, 3: 0.80, 4: 0.90, 5: 0.90}
_BLEND_FLOOR_GAMES = 6

PROP_STAT_MAP = {
    "Passing Yards":     "passing_yards",
    "Passing TDs":       "passing_tds",
    "Interceptions":     "passing_interceptions",
    "Pass Attempts":     "attempts",
    "Completions":       "completions",
    "Rushing Yards":     "rushing_yards",
    "Rushing Attempts":  "carries",
    "Receiving Yards":   "receiving_yards",
    "Receptions":        "receptions",
    "Targets":           "targets",
}
PROP_POSITION_MAP = {
    "Passing Yards":    ["QB"],
    "Passing TDs":      ["QB"],
    "Interceptions":    ["QB"],
    "Pass Attempts":    ["QB"],
    "Completions":      ["QB"],
    "Rushing Yards":    ["RB", "QB", "WR"],
    "Rushing Attempts": ["RB", "QB", "WR"],
    "Receiving Yards":  ["WR", "TE", "RB"],
    "Receptions":       ["WR", "TE", "RB"],
    "Targets":          ["WR", "TE", "RB"],
}
PROP_AVG_LABEL = {
    "Passing Yards":    "Avg Pass Yds",
    "Passing TDs":      "Avg Pass TDs",
    "Interceptions":    "Avg INT",
    "Pass Attempts":    "Avg Pass Att",
    "Completions":      "Avg Completions",
    "Rushing Yards":    "Avg Rush Yds",
    "Rushing Attempts": "Avg Carries",
    "Receiving Yards":  "Avg Rec Yds",
    "Receptions":       "Avg Rec",
    "Targets":          "Avg Targets",
}
# Prop Leaderboard sample-size options only (core/nflverse_data.py::
# build_prop_leaderboard / tabs/prop_leaderboard.py::render_leaderboard_view).
# Not used by Player Research's own separate Sample Size control (that
# one still offers Last 10 Games, unaffected by this).
SAMPLE_OPTIONS = {"Last 3 Games": 3, "Last 5 Games": 5, "Season": None}
_SEASON_MIN_GAMES = 3

PLAYER_SEARCH_EXTRA_STATS = {"Anytime TD": "_any_td"}

PROP_LABEL_TO_ODDS_MARKET = {
    "Passing Yards":    "player_pass_yds",
    "Passing TDs":      "player_pass_tds",
    "Interceptions":    "player_pass_interceptions",
    "Pass Attempts":    "player_pass_attempts",
    "Completions":      "player_pass_completions",
    "Rushing Yards":    "player_rush_yds",
    "Rushing Attempts": "player_rush_attempts",
    "Receiving Yards":  "player_reception_yds",
    "Receptions":       "player_receptions",
    "Targets":          None,
    "Anytime TD":       "player_anytime_td",
}

DEBUG_SEASON = False  # temporary — confirms real get_current_season() value


def _norm_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower().strip()
    for suffix in (" jr.", " jr", " sr.", " sr", " ii", " iii", " iv"):
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
    return n


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _fetch_player_stats(season: int):
    """Raises on failure so st.cache_data never caches a transient
    nflreadpy/network failure as if it were a genuine "no stats" result —
    same pattern/rationale as core/lineup_data.py::get_players_by_team's
    fix (a season's first load in a session could hit a blip and get
    stuck returning nothing for the full TTL otherwise)."""
    return nfl.load_player_stats(seasons=season, summary_level="week")


def _load_player_stats(season: int):
    if not _NFLVERSE_AVAILABLE:
        return None
    try:
        return _fetch_player_stats(season)
    except Exception:
        return None


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _fetch_team_stats(season: int):
    """See _fetch_player_stats above — same failure-isolation rationale."""
    return nfl.load_team_stats(seasons=season, summary_level="week")


def _load_team_stats(season: int):
    if not _NFLVERSE_AVAILABLE:
        return None
    try:
        return _fetch_team_stats(season)
    except Exception:
        return None


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def _fetch_teams():
    """See _fetch_player_stats above — same failure-isolation rationale."""
    return nfl.load_teams()


def _load_teams():
    if not _NFLVERSE_AVAILABLE:
        return None
    try:
        return _fetch_teams()
    except Exception:
        return None


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _load_schedules(season: int):
    if not _NFLVERSE_AVAILABLE:
        return None
    try:
        return nfl.load_schedules(seasons=season)
    except Exception:
        return None


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _load_injuries(season: int):
    if not _NFLVERSE_AVAILABLE:
        return None
    try:
        return nfl.load_injuries(seasons=season)
    except Exception:
        return None


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _fetch_current_season() -> int:
    """See _fetch_player_stats above — same failure-isolation rationale.
    get_current_season() is called several times per Player Research
    render (Player Card, Prop Analysis, and — indirectly, via
    get_usage_samples/get_recent_games/get_player_game_log — Player
    Context all need it) and again on every unrelated rerun; caching it
    avoids repeating nfl.get_current_season() for no reason."""
    return nfl.get_current_season()


def get_current_season() -> int:
    if not _NFLVERSE_AVAILABLE:
        return None
    try:
        season = _fetch_current_season()
    except Exception:
        return None
    if DEBUG_SEASON:
        st.caption(f"debug: nflreadpy.get_current_season() = {season!r}")
    return season


def _week_label(week, season, current_season):
    """Delegates to the single shared formatter in core.nfl_player_context
    so there is exactly one week-label implementation, not a second one
    living inside this module."""
    from core.nfl_player_context import format_nfl_week
    return format_nfl_week(week, season, current_season)


def recency_weighted_average(values: list, decay: float = RECENCY_DECAY):
    pairs = [(i, v) for i, v in enumerate(values) if v is not None]
    if not pairs:
        return None
    n = len(values)
    weighted_sum, weight_total = 0.0, 0.0
    for i, v in pairs:
        games_ago = (n - 1) - i
        w = decay ** games_ago
        weighted_sum += v * w
        weight_total += w
    return round(weighted_sum / weight_total, 2) if weight_total > 0 else None


def blend_prior_season(current_value, prior_value, games_played: int, continuity: bool = True):
    if current_value is None and prior_value is None:
        return None
    if not continuity or prior_value is None or games_played >= _BLEND_FLOOR_GAMES:
        return current_value
    if current_value is None:
        return prior_value
    current_weight = _BLEND_SCHEDULE.get(games_played, 0.90)
    return round(current_value * current_weight + prior_value * (1 - current_weight), 2)


def _build_weekly_rows(player_stats, team_stats, player_name: str, team_abbr: str):
    if player_stats is None:
        return []
    try:
        target_name = _norm_name(player_name)
        rows = player_stats.filter(player_stats["team"] == team_abbr).to_dicts()
        rows = [r for r in rows if _norm_name(r.get("player_display_name") or r.get("player_name") or "") == target_name]
        rows.sort(key=lambda r: (r.get("season", 0), r.get("week", 0)))

        team_carries_by_week = {}
        if team_stats is not None:
            for r in team_stats.filter(team_stats["team"] == team_abbr).to_dicts():
                team_carries_by_week[(r["season"], r["week"])] = r.get("carries")

        for r in rows:
            _team_carries = team_carries_by_week.get((r.get("season"), r.get("week")))
            r["_carry_share"] = (
                round(r["carries"] / _team_carries, 3)
                if r.get("carries") is not None and _team_carries else None
            )
            _rush_td = r.get("rushing_tds")
            _rec_td = r.get("receiving_tds")
            if _rush_td is not None or _rec_td is not None:
                r["_total_td"] = (_rush_td or 0) + (_rec_td or 0)
            else:
                r["_total_td"] = None
        return rows
    except Exception:
        return []


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _weekly_rows_for_player(season: int, player_name: str, team_abbr: str) -> list[dict]:
    """Memoized per (season, player, team) — get_usage_samples,
    get_recent_games, and get_player_game_log each independently need
    this exact result for the same player within a single Player Research
    render (Player Card, Prop Analysis, and Player Context all call one of
    them), and again on every unrelated rerun of that page. _load_player_
    stats/_load_team_stats are already cached, but the per-player
    filter/to_dicts/derive-fields work in _build_weekly_rows was being
    redone from scratch on every one of those calls. Caching by the three
    plain (hashable) identifying values, rather than caching
    _build_weekly_rows itself keyed on its DataFrame arguments, avoids
    st.cache_data having to hash the full season DataFrame on every call.

    Raises if the underlying season data isn't available, rather than
    quietly returning [] — _load_player_stats/_load_team_stats already
    isolate a transient nflreadpy failure from a cached None (see
    _fetch_player_stats above), but only if that failure signal actually
    reaches this function's own st.cache_data decorator as an exception;
    otherwise a transient blip on the very first call for a player would
    get cached here as "this player has no rows" for the full TTL.
    _NFLVERSE_AVAILABLE is already checked before any caller reaches this
    function, so a None here can only mean the fetch itself failed, not
    that the package is unavailable."""
    player_stats = _load_player_stats(season)
    if player_stats is None:
        raise RuntimeError(f"player stats unavailable for season {season}")
    return _build_weekly_rows(player_stats, _load_team_stats(season), player_name, team_abbr)


def _team_abbr_for(team_full_name: str, teams_df) -> str | None:
    if teams_df is None or not team_full_name:
        return None
    try:
        matches = teams_df.filter(teams_df["team_name"] == team_full_name)
        if matches.height == 0:
            return None
        return matches["team_abbr"][0]
    except Exception:
        return None


def get_nfl_team_names() -> list[tuple[str, str]]:
    """(full_name, abbr) pairs for every NFL team, sourced from the same
    already-cached nflreadpy team table (_load_teams(), 24h TTL) used
    elsewhere in this module — no new fetch. abbr matches exactly what
    build_prop_leaderboard's result rows carry in "team" (both come from
    nflreadpy), so this is the correct source for a Team filter against
    those rows — not core/lineup_data.py's NFL_TEAMS, which uses ESPN's
    own (sometimes differently-cased/abbreviated) team codes."""
    teams_df = _load_teams()
    if teams_df is None:
        return []
    try:
        rows = teams_df.to_dicts()
    except Exception:
        return []
    pairs = {
        (r.get("team_name"), r.get("team_abbr"))
        for r in rows if r.get("team_name") and r.get("team_abbr")
    }
    return sorted(pairs)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _current_week_teams_for_season(season: int, week_index: int) -> set:
    """Team abbreviations with a game anywhere in the given NFL week
    (Thu-Wed) per nflreadpy's own schedule -- includes BOTH completed
    and upcoming games in that week (a team that already played earlier
    in the week is still included; this never narrows to "today" or
    "still upcoming"). Bye teams simply have no row for that week and are
    naturally excluded. week_index is the same value infer_current_week_
    index (core/data_sources.py) already returns for every other
    current-week view in this app -- not a second NFL calendar."""
    schedules = _load_schedules(season)
    if schedules is None:
        return set()
    try:
        wk = schedules.filter(schedules["week"] == week_index)
        if "game_type" in wk.columns:
            wk = wk.filter(wk["game_type"] == "REG")
        rows = wk.to_dicts()
    except Exception:
        return set()
    teams = set()
    for r in rows:
        if r.get("home_team"):
            teams.add(r["home_team"])
        if r.get("away_team"):
            teams.add(r["away_team"])
    return teams


def get_current_week_teams(now_utc) -> set:
    """Public entry point: resolves season/week from now_utc via the same
    infer_current_week_index used everywhere else in the app, then
    delegates to the cached per-(season, week_index) lookup above."""
    season = get_current_season()
    if season is None:
        return set()
    week_index = infer_current_week_index(now_utc)
    return _current_week_teams_for_season(season, week_index)


def get_current_week_team_names(now_utc) -> list[tuple[str, str]]:
    """(full_name, abbr) pairs restricted to only the teams with a game
    anywhere in the current NFL week. The Team filter dropdown
    (tabs/prop_leaderboard.py) and Prop Leaderboard candidate eligibility
    (_cached_build_prop_leaderboard below) both derive from this exact
    same get_current_week_teams set, so they can never disagree -- a bye
    team is never offered as a filter option that could only return zero
    results. Fails open to the full team list if the current-week team
    set can't be resolved (e.g. a transient schedules fetch failure),
    rather than emptying the Team filter entirely."""
    current_teams = get_current_week_teams(now_utc)
    all_pairs = get_nfl_team_names()
    if not current_teams:
        return all_pairs
    return [(name, abbr) for name, abbr in all_pairs if abbr in current_teams]


def get_player_usage(player_name: str, team_full_name: str, position: str, prior_team_full_name: str | None = None) -> dict:
    if not _NFLVERSE_AVAILABLE:
        return {}
    try:
        season = get_current_season()
        if season is None:
            return {}
        teams_df = _load_teams()
        team_abbr = _team_abbr_for(team_full_name, teams_df)
        if not team_abbr:
            return {}

        cur_stats = _load_player_stats(season)
        cur_team_stats = _load_team_stats(season)
        cur_rows = _build_weekly_rows(cur_stats, cur_team_stats, player_name, team_abbr)
        games_played = len(cur_rows)

        continuity = True
        if prior_team_full_name and prior_team_full_name != team_full_name:
            continuity = False

        prior_rows = []
        if games_played < _BLEND_FLOOR_GAMES:
            prior_stats = _load_player_stats(season - 1)
            prior_team_stats = _load_team_stats(season - 1)
            prior_rows = _build_weekly_rows(prior_stats, prior_team_stats, player_name, team_abbr)

        def _series(field):
            return [r.get(field) for r in cur_rows]

        def _prior_avg(field):
            vals = [r.get(field) for r in prior_rows if r.get(field) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        def _season_avg(field):
            vals = [r.get(field) for r in cur_rows if r.get(field) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        def _metric(field, per_game_source=None):
            source = field if per_game_source is None else per_game_source
            season_val = _season_avg(source)
            current_val = recency_weighted_average(_series(source))
            prior_val = _prior_avg(source)
            blended = blend_prior_season(current_val, prior_val, games_played, continuity)
            return {"season": season_val, "current_role": blended, "games_played": games_played}

        result = {"games_played": games_played, "position": position}
        if position in ("WR", "TE"):
            result["targets_per_game"] = _metric("targets")
            result["target_share"] = _metric("target_share")
            result["receiving_yards_per_game"] = _metric("receiving_yards")
        elif position == "RB":
            result["carries_per_game"] = _metric("carries")
            result["carry_share"] = _metric("_carry_share")
            result["targets_per_game"] = _metric("targets")
        elif position == "QB":
            result["attempts_per_game"] = _metric("attempts")
            result["rush_attempts_per_game"] = _metric("carries")
            result["passing_yards_per_game"] = _metric("passing_yards")
        return result
    except Exception:
        return {}


def get_usage_samples(player_name: str, team_full_name: str, position: str, metrics: list[str] | None = None) -> dict:
    if not _NFLVERSE_AVAILABLE:
        return {}
    try:
        season = get_current_season()
        if season is None:
            return {}
        teams_df = _load_teams()
        team_abbr = _team_abbr_for(team_full_name, teams_df)
        if not team_abbr:
            return {}
        rows = _weekly_rows_for_player(season, player_name, team_abbr)
        if not rows:
            return {}

        windows = {"season": rows, "last5": rows[-5:], "last3": rows[-3:]}
        metric_list = metrics if metrics is not None else LINEUP_USAGE_METRICS.get(position, [])

        result = {"games_played": len(rows)}
        for m in metric_list:
            field = _METRIC_FIELD.get(m)
            entry = {}
            for wname, wrows in windows.items():
                vals = [r.get(field) for r in wrows if r.get(field) is not None]
                entry[wname] = {"value": round(sum(vals) / len(vals), 2) if vals else None, "games": len(vals)}
            result[m] = entry
        return result
    except Exception:
        return {}


def get_card_season_stats(player_name: str, team_full_name: str, position: str) -> list[dict]:
    metric_list = CARD_SEASON_METRICS.get(position, [])
    if not metric_list:
        return []
    samples = get_usage_samples(player_name, team_full_name, position, metrics=metric_list)
    if not samples or samples.get("games_played", 0) == 0:
        return []
    out = []
    for m in metric_list:
        season_val = samples.get(m, {}).get("season", {}).get("value")
        if season_val is None:
            continue
        out.append({"label": CARD_METRIC_LABELS.get(m, m), "value": season_val})
    return out


def get_expanded_season_stats(player_name: str, team_full_name: str, position: str) -> dict:
    metric_list = EXPANDED_SEASON_METRICS.get(position, [])
    if not metric_list:
        return {}
    samples = get_usage_samples(player_name, team_full_name, position, metrics=metric_list)
    if not samples or samples.get("games_played", 0) == 0:
        return {}

    row = {"Games": samples["games_played"]}
    for m in metric_list:
        val = samples.get(m, {}).get("season", {}).get("value")
        label = METRIC_LABELS.get(m, m)
        if m in PERCENT_METRICS:
            row[label] = f"{val * 100:.0f}%" if val is not None else "—"
        else:
            row[label] = val if val is not None else "—"

    def _get(m):
        return samples.get(m, {}).get("season", {}).get("value")

    if position == "QB":
        att, comp = _get("attempts_per_game"), _get("completions_per_game")
        row["Comp %"] = f"{comp / att * 100:.1f}%" if att and comp is not None else "—"
    elif position == "RB":
        car, ry = _get("carries_per_game"), _get("rush_yards_per_game")
        row["YPC"] = round(ry / car, 1) if car and ry is not None else "—"
    elif position in ("WR", "TE"):
        rec, ry = _get("receptions_per_game"), _get("receiving_yards_per_game")
        row["Yds/Rec"] = round(ry / rec, 1) if rec and ry is not None else "—"

    return row


def get_recent_games(player_name: str, team_full_name: str, position: str, n: int = 5) -> list[dict]:
    if not _NFLVERSE_AVAILABLE:
        return []
    try:
        season = get_current_season()
        if season is None:
            return []
        teams_df = _load_teams()
        team_abbr = _team_abbr_for(team_full_name, teams_df)
        if not team_abbr:
            return []
        rows = _weekly_rows_for_player(season, player_name, team_abbr)
        if not rows:
            return []
        rows_sorted = sorted(rows, key=lambda r: (r.get("season") or 0, r.get("week") or 0), reverse=True)[:n]

        out = []
        for r in rows_sorted:
            row = {"Week": _week_label(r.get("week"), r.get("season", season), season), "Opponent": r.get("opponent_team", "—")}
            if position in ("WR", "TE"):
                row.update({
                    "Targets": r.get("targets"), "Receptions": r.get("receptions"),
                    "Receiving Yards": r.get("receiving_yards"), "TD": r.get("receiving_tds"),
                })
            elif position == "RB":
                row.update({
                    "Carries": r.get("carries"), "Rush Yards": r.get("rushing_yards"),
                    "Targets": r.get("targets"), "Receptions": r.get("receptions"), "Rec Yds": r.get("receiving_yards"),
                })
            elif position == "QB":
                row.update({
                    "Pass Att": r.get("attempts"), "Pass Yds": r.get("passing_yards"),
                    "Pass TD": r.get("passing_tds"), "INT": r.get("passing_interceptions"),
                    "Rush Att": r.get("carries"), "Rush Yds": r.get("rushing_yards"),
                })
            out.append(row)
        return out
    except Exception:
        return []


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _season_rows_for_player_by_name(season: int, player_name: str) -> list[dict]:
    """
    Same season-scoped weekly rows as _weekly_rows_for_player, but
    matched by player name only -- no team_abbr filter. Used exclusively
    by get_player_game_log's cross-season lookback (below) to fetch the
    PRIOR season: a player may have been on a different team last season
    than their current one, and filtering prior-season rows by the
    player's CURRENT team abbreviation (as _weekly_rows_for_player does)
    would incorrectly exclude every one of their prior-season games.

    Does not compute _carry_share/_total_td (the team-scoped derived
    fields _build_weekly_rows adds) -- no prop stat sourced through
    get_player_game_log needs them; Anytime TD is derived directly from
    raw rushing_tds/receiving_tds/passing_tds below, not from
    _total_td. Raises on a genuine fetch failure, mirroring
    _weekly_rows_for_player / _all_player_weekly_rows, so a transient
    nflreadpy blip is never cached as "no rows this season."
    """
    player_stats = _load_player_stats(season)
    if player_stats is None:
        raise RuntimeError(f"player stats unavailable for season {season}")
    target_name = _norm_name(player_name)
    rows = player_stats.to_dicts()
    rows = [r for r in rows if _norm_name(r.get("player_display_name") or r.get("player_name") or "") == target_name]
    rows.sort(key=lambda r: (r.get("season", 0), r.get("week", 0)))
    return rows


def get_player_game_log(
    player_name: str, team_full_name: str, stat_field: str, n_games: int | None = 10,
    cross_season: bool = False,
) -> list[dict]:
    """
    cross_season=True (only meaningful when n_games is a specific
    number, e.g. Prop Research's "Last 5/10 Games" sample size) reaches
    back into the immediately prior season for just enough of its most
    recent games to fill the requested count, when the current season
    alone doesn't have n_games worth of data yet -- the early-season
    case where e.g. only 2 current-season games exist and the user asked
    for the Last 10. Defaults to False, and n_games=None always skips
    this regardless of the flag: both preserve the exact prior behavior
    for every other caller, in particular the Current-Season Average
    fallback (called with n_games=None), which must stay current-season
    only by design -- distinct from this historical hit-rate sample,
    which is allowed to cross the season boundary.
    """
    if not _NFLVERSE_AVAILABLE:
        return []
    try:
        season = get_current_season()
        if season is None:
            return []
        teams_df = _load_teams()
        team_abbr = _team_abbr_for(team_full_name, teams_df)
        if not team_abbr:
            return []
        rows = _weekly_rows_for_player(season, player_name, team_abbr)

        def _extract(rows_list, fallback_season):
            out = []
            for r in rows_list:
                if stat_field == "_any_td":
                    rush_td = r.get("rushing_tds") or 0
                    rec_td = r.get("receiving_tds") or 0
                    pass_td = r.get("passing_tds") or 0
                    val = 1.0 if (rush_td + rec_td + pass_td) > 0 else 0.0
                else:
                    val = r.get(stat_field)
                if val is None:
                    continue
                out.append({
                    "week": r.get("week"), "opponent": r.get("opponent_team", "—"),
                    "value": float(val), "season": r.get("season", fallback_season),
                })
            return out

        out = _extract(rows, season) if rows else []
        out.sort(key=lambda x: (x["season"] or 0, x["week"] or 0), reverse=True)

        # Sort key is (season, week), not week alone, so Week 18 of the
        # prior season never outranks Week 1 of the current one. Every
        # prior-season (season, week) tuple sorts strictly below every
        # current-season one, so appending the prior season's own
        # most-recent-first slice after the current season's rows keeps
        # the combined list correctly ordered without a full re-sort.
        if cross_season and n_games and len(out) < n_games:
            _needed = n_games - len(out)
            try:
                prior_rows = _season_rows_for_player_by_name(season - 1, player_name)
            except Exception:
                prior_rows = []
            prior_out = _extract(prior_rows, season - 1) if prior_rows else []
            prior_out.sort(key=lambda x: (x["season"] or 0, x["week"] or 0), reverse=True)
            out.extend(prior_out[:_needed])

        return out[:n_games] if n_games else out
    except Exception:
        return []


def calculate_hit_rate(game_log: list[dict], line: float, side: str = "Over") -> dict | None:
    if not game_log:
        return None
    hits = misses = pushes = 0
    for g in game_log:
        if g["value"] == line:
            pushes += 1
        elif (g["value"] > line if side == "Over" else g["value"] < line):
            hits += 1
        else:
            misses += 1
    decided = hits + misses
    if decided == 0:
        return None
    return {"hits": hits, "total": decided, "pushes": pushes}


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _all_player_weekly_rows(season: int) -> list[dict]:
    """Raises if the underlying season data isn't available, mirroring
    _weekly_rows_for_player's own fix above — st.cache_data must never
    cache a transient nflreadpy failure as "no players this season" for
    the full 6h TTL. Previously called _load_player_stats, which
    swallows any failure into None, so a blip on the very first call for
    a season got a [] result cached here for 6 hours, indistinguishable
    from a genuine (but nonexistent) no-data season."""
    player_stats = _load_player_stats(season)
    if player_stats is None:
        raise RuntimeError(f"player stats unavailable for season {season}")
    try:
        return player_stats.to_dicts()
    except Exception:
        return []


def build_prop_leaderboard(
    stat_label: str, side: str, line: float, sample_label: str, now_utc, limit: int | None = 10,
) -> list[dict]:
    """
    Returns up to `limit` results (10 by default, matching prior behavior
    exactly); pass limit=None for the full sorted candidate list, e.g. so
    a caller can filter (by team/position) before truncating to a top-N
    for display, rather than filtering an already-truncated top 10 and
    getting sparse or misleading results. Candidate construction,
    eligibility, hit-rate calculation, and ranking are unchanged — this
    only makes the final truncation step optional.

    now_utc resolves only the current NFL week (via the same
    infer_current_week_index used everywhere else in the app) for
    current-week candidate eligibility — see _cached_build_prop_leaderboard.
    It plays no role in which historical games make up a player's rolling
    sample; that's governed entirely by sample_label/n_games.

    This outer wrapper stays uncached and resolves the season first,
    exactly like get_current_season()'s own callers elsewhere in this
    module — a transient failure here must be retried on the next rerun,
    not cached as an empty leaderboard for hours. The actual league-wide
    scan is delegated to _cached_build_prop_leaderboard below, which IS
    cached (and can raise on a genuine data-fetch failure, caught here).
    """
    season = get_current_season()
    if season is None:
        return []
    week_index = infer_current_week_index(now_utc)
    try:
        return _cached_build_prop_leaderboard(season, stat_label, side, line, sample_label, week_index, limit)
    except Exception:
        return []


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _cached_build_prop_leaderboard(
    season: int, stat_label: str, side: str, line: float, sample_label: str, week_index: int, limit: int | None,
) -> list[dict]:
    """
    Scans every player-week row for the whole league (_all_player_weekly_rows,
    itself cached and failure-isolated) and was being redone from scratch
    on every rerun of Prop Leaderboard's render — including reruns
    triggered by an unrelated widget elsewhere in the app — even though
    its own inputs only change when the user actually edits a filter and
    submits. TTL matches _all_player_weekly_rows' own 6h TTL, so this
    adds no staleness beyond what's already accepted for the underlying
    data. season/week_index are passed in already-resolved by
    build_prop_leaderboard above rather than re-derived here, so a
    season/week lookup failure can never be masked by this function's
    own cache.

    "Season" (n_games is None) is current-season only, unchanged from
    before — _SEASON_MIN_GAMES eligibility, no prior-season data touched
    or fetched at all.

    "Last 3 Games" / "Last 5 Games" are rolling samples that may reach
    into the prior season, mirroring get_player_game_log's own
    cross-season behavior (Player Research's own sample-size control),
    applied here to the league-wide scan instead of one player at a
    time: a player with fewer than n_games games so far this season is
    topped up with their most recent prior-season games rather than
    being excluded outright. As the current season accumulates games,
    prior-season games naturally stop being needed and drop out on
    their own — nothing here special-cases that, it falls out of always
    taking the N most recent games by (season, week) once enough
    current-season games exist.

    Grouped by normalized player NAME ONLY (not name+team), via the same
    _norm_name already used elsewhere in this module to match a player
    across a team change — grouping by (name, team) would silently
    fragment a traded player's history into two separate, incomplete
    entries instead of merging it. The displayed "team" is whichever
    team the player's own most recent qualifying game was played for —
    never a stale/prior team once current games exist — and that same
    most-recent team is also what current-week eligibility (below) is
    checked against.

    Players with zero current-season games in an eligible position are
    not considered at all, even if prior-season data alone could fill a
    sample — this keeps the leaderboard scoped to players with some
    current-season presence.

    Current-week eligibility (new): a candidate's CURRENT/latest team
    (the same most-recent-game team used for display) must have a game
    anywhere in the current NFL week (get_current_week_teams /
    _current_week_teams_for_season) — completed or upcoming, so a team
    that already played earlier in the week stays eligible through the
    rest of it, and a bye team is excluded. This is entirely separate
    from, and never filters, the historical sample itself: which games
    count toward Last 3/Last 5/Season is unaffected by this check. Fails
    open (no filtering at all) if the current-week team set can't be
    resolved, rather than silently emptying the whole leaderboard.
    """
    field = PROP_STAT_MAP.get(stat_label)
    eligible_positions = PROP_POSITION_MAP.get(stat_label, [])
    n_games = SAMPLE_OPTIONS.get(sample_label)
    if not field or not eligible_positions:
        return []

    current_rows = _all_player_weekly_rows(season)
    if not current_rows:
        return []

    by_player: dict[str, list[dict]] = {}
    for r in current_rows:
        if r.get("position") not in eligible_positions:
            continue
        val = r.get(field)
        if val is None:
            continue
        raw_name = r.get("player_display_name") or r.get("player_name")
        key = _norm_name(raw_name)
        by_player.setdefault(key, []).append({
            "week": r.get("week"), "season": season, "value": float(val),
            "team": r.get("team"), "player": raw_name, "position": r.get("position"),
        })

    if n_games is not None:
        # Only fetch the prior season at all if at least one candidate
        # actually needs it — once far enough into a season that
        # everyone already has a full current-season sample, this never
        # touches season - 1.
        needs_prior = {k for k, games in by_player.items() if len(games) < n_games}
        if needs_prior:
            try:
                prior_rows = _all_player_weekly_rows(season - 1)
            except Exception:
                prior_rows = []
            for r in prior_rows:
                val = r.get(field)
                if val is None:
                    continue
                raw_name = r.get("player_display_name") or r.get("player_name")
                key = _norm_name(raw_name)
                if key not in needs_prior:
                    continue  # only backfill players already seen this season
                by_player[key].append({
                    "week": r.get("week"), "season": season - 1, "value": float(val),
                    "team": r.get("team"), "player": raw_name, "position": r.get("position"),
                })

    current_week_teams = _current_week_teams_for_season(season, week_index)

    results = []
    for key, games in by_player.items():
        # Chronological ordering by (season, week) — never raw API row
        # order — so "most recent N" is unambiguous and current-season
        # games always outrank prior-season ones regardless of week number.
        games.sort(key=lambda g: (g["season"], g["week"] or 0), reverse=True)

        if n_games is not None:
            if len(games) < n_games:
                continue
            sample = games[:n_games]
        else:
            if len(games) < _SEASON_MIN_GAMES:
                continue
            sample = games

        hr = calculate_hit_rate(sample, line, side)
        if not hr:
            continue

        most_recent = sample[0]
        if current_week_teams and most_recent["team"] not in current_week_teams:
            continue

        results.append({
            "player": most_recent["player"], "team": most_recent["team"], "position": most_recent["position"],
            "hits": hr["hits"], "games": hr["total"], "pushes": hr["pushes"],
            "hit_rate": hr["hits"] / hr["total"] * 100,
            "avg": round(sum(g["value"] for g in sample) / len(sample), 1),
        })

    results.sort(key=lambda r: (r["hit_rate"], r["hits"], r["games"]), reverse=True)
    return results[:limit] if limit is not None else results
