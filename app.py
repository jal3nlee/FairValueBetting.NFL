# app.py
import os
from datetime import datetime, timezone
import streamlit as st
from pathlib import Path
from PIL import Image
from supabase import create_client, Client
from streamlit_cookies_manager import EncryptedCookieManager
from core.data_sources import infer_current_week_index
from tabs import (
    fair_value_model,
    matchup_center,
    lineup_comparison,
    fantasy_draft,
    prop_leaderboard,
    sportsbook_screener,
    parlay_builder,
    arbitrage_tracker,
)
# =======================
# AUTH
# =======================
SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
COOKIE_SECRET     = os.getenv("COOKIE_SECRET", "")
if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    st.error("Auth not configured. Add SUPABASE_URL and SUPABASE_ANON_KEY to environment.")
    st.stop()
if not COOKIE_SECRET:
    st.error("COOKIE_SECRET is not set. Add it to your environment variables.")
    st.stop()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
cookies = EncryptedCookieManager(prefix="fvb_", password=COOKIE_SECRET)
if not cookies.ready():
    st.stop()
st.session_state.setdefault("sb_access_token", None)
st.session_state.setdefault("sb_refresh_token", None)
st.session_state.setdefault("sb_session", None)
if (
    cookies.get("access_token")
    and cookies.get("refresh_token")
    and not st.session_state.get("sb_access_token")
):
    try:
        supabase.auth.set_session(
            access_token=cookies.get("access_token"),
            refresh_token=cookies.get("refresh_token"),
        )
        st.session_state.sb_access_token = cookies.get("access_token")
        st.session_state.sb_refresh_token = cookies.get("refresh_token")
    except Exception as e:
        st.warning(f"Could not restore login session: {e}")
def save_session(sess, remember=False):
    st.session_state.sb_session = sess
    st.session_state.sb_access_token = sess.access_token
    st.session_state.sb_refresh_token = sess.refresh_token
    if remember:
        cookies["access_token"] = sess.access_token
        cookies["refresh_token"] = sess.refresh_token
        cookies.save()
def clear_session():
    st.session_state.sb_session = None
    st.session_state.sb_access_token = None
    st.session_state.sb_refresh_token = None
    cookies.clear()
    cookies.save()
authed = bool(
    st.session_state.sb_access_token and st.session_state.sb_refresh_token
)
# =======================
# BRANDING
# =======================
ROOT       = Path(__file__).parent.resolve()
ASSET_DIRS = [ROOT / "assets", ROOT / ".streamlit" / "assets"]
def find_asset(name: str):
    for d in ASSET_DIRS:
        p = d / name
        if p.is_file():
            return p
    return None
def newest_favicon():
    cands = []
    for d in ASSET_DIRS:
        if d.is_dir():
            cands += list(d.glob("favicon*.png"))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)
LOGO_PATH    = find_asset("logo.png")
FAVICON_PATH = newest_favicon()
favicon_img = None
if FAVICON_PATH:
    try:
        favicon_img = Image.open(FAVICON_PATH)
    except Exception:
        favicon_img = None
st.set_page_config(
    page_title="Fair Value Betting",
    page_icon=(favicon_img if favicon_img else "🏈"),
    layout="wide",
    initial_sidebar_state="expanded",
)
SIDEBAR_W = 320
DEBUG_MODE = False
# =======================
# SIDEBAR UI
# =======================
with st.sidebar:
    if LOGO_PATH:
        st.image(str(LOGO_PATH), width=SIDEBAR_W)
    else:
        st.title("Fair Value Betting")
    st.markdown(
        "[fairvaluebetting.com](https://fairvaluebetting.com)  ·  "
        "⚾ [MLB](https://mlb.fairvaluebetting.com)  ·  "
        "🏈 [NCAAF](https://ncaaf.fairvaluebetting.com)"
    )
    st.sidebar.divider()
    if authed:
        user = supabase.auth.get_user()
        user_email = (
            getattr(user.user, "email", None)
            if user and getattr(user, "user", None)
            else None
        )
        st.success(f"Signed in{f' as {user_email}' if user_email else ''}.")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Log out", use_container_width=True):
                try:
                    supabase.auth.sign_out()
                except Exception:
                    pass
                st.session_state.clear()
                cookies.clear()
                cookies.save()
                st.rerun()
    else:
        st.info(
            "Free full access in September — create a free account to unlock filters and sorting."
        )
        with st.form("login_form_sidebar", clear_on_submit=False, border=True):
            email       = st.text_input("Email", key="signin_email_sidebar")
            password    = st.text_input("Password", type="password", key="signin_pw_sidebar")
            remember_me = st.checkbox("Keep me logged in", value=True)
            submit      = st.form_submit_button("Sign in", use_container_width=True)
        if submit:
            try:
                res  = supabase.auth.sign_in_with_password(
                    {"email": (email or "").strip(), "password": password}
                )
                sess = getattr(res, "session", None)
                if sess:
                    save_session(sess, remember=remember_me)
                    st.success("Signed in successfully.")
                    st.rerun()
                else:
                    st.error("Sign-in succeeded but no session returned.")
            except Exception as e:
                if "Invalid login credentials" in str(e):
                    st.error("Invalid email or password.")
                else:
                    st.error(f"Sign-in failed: {e}")
        with st.expander("Create account", expanded=False):
            full_name = st.text_input("Name", key="signup_name_sidebar")
            email2    = st.text_input("Email", key="signup_email_sidebar")
            pw2       = st.text_input("Password", type="password", key="signup_pw_sidebar")
            submit2   = st.button("Create Account", use_container_width=True)
            if submit2:
                if not full_name.strip():
                    st.warning("Please enter your full name.")
                elif not email2 or not pw2:
                    st.warning("Email and password are required.")
                else:
                    try:
                        supabase.auth.sign_up(
                            {
                                "email": email2.strip(),
                                "password": pw2,
                                "options": {"data": {"full_name": full_name.strip()}},
                            }
                        )
                        st.success("Account created! Check your email to verify, then sign in.")
                    except Exception as e:
                        st.error(f"Sign-up failed: {str(e) or 'Try again.'}")
with st.sidebar.expander("How to use", expanded=False):
    st.markdown(
        """
1. **Fair Value Model** — starts with a Market Movers snapshot of today's slate and
   the top EV plays, then lets you pick a Date Range and Market, filter by Expected
   Value and Odds, and compare Best Odds against our Fair Odds estimate.
2. **Matchup Center** — dig into any individual game's market snapshot and research.
3. **Lineup Analysis** — research weekly usage, props, game environment, and matchup context for one player, or compare up to four.
4. **Fantasy Draft** — consensus ADP rankings across platforms, filterable by position.
5. **Prop Research** — research an individual player's props, season stats, recent
   games, and matchup context directly, or find the top players on the best current
   hit-rate streak for a selected prop.
6. **Sportsbook Screener** — pure line shopping across every sportsbook.
7. **Parlay Builder** — build and compare multi-leg parlays across sportsbooks.
8. **Arbitrage Tracker** — scan current prices for markets where the best price on
   each side, across different books, guarantees a profit regardless of outcome.
        """
    )
with st.sidebar.expander("Glossary", expanded=False):
    st.markdown(
        """
**EV% (Expected Value %)** — How favorable the offered price is versus the fair baseline (no-vig).  
**Fair Odds** — The American-odds equivalent of the weighted, no-vig sportsbook consensus.
        """
    )
with st.sidebar.expander("Feedback", expanded=False):
    _fb_user = None
    try:
        _fb_user = getattr(st.session_state.get("sb_session", None), "user", None)
    except Exception:
        _fb_user = None
    if not _fb_user:
        st.info("You must be signed in to leave feedback.")
    else:
        with st.form("feedback_form", clear_on_submit=True):
            _full_name  = (_fb_user.user_metadata or {}).get("full_name") or (_fb_user.user_metadata or {}).get("name") or ""
            _email_addr = getattr(_fb_user, "email", "") or (_fb_user.user_metadata or {}).get("email", "")
            st.markdown(f"**Submitting as:** {_full_name or 'Unknown'}  \n**Email:** {_email_addr or 'Unknown'}")
            feedback_text = st.text_area("Share your thoughts, ideas, or issues:")
            submitted     = st.form_submit_button("Submit Feedback")
        if submitted:
            txt = (feedback_text or "").strip()
            if not txt:
                st.warning("Please enter feedback before submitting.")
            else:
                try:
                    supabase.table("feedback").insert(
                        {
                            "message": txt,
                            "name":    _full_name.strip() or None,
                            "email":   (_email_addr or "").strip() or None,
                            "user_id": _fb_user.id,
                        }
                    ).execute()
                    st.success("Thanks for your feedback!")
                except Exception as e:
                    st.error(f"Error saving feedback: {e}")
with st.sidebar.expander("Disclaimer", expanded=False):
    st.markdown(
        """
**Fair Value Betting** is for **education and entertainment** only — not financial or betting advice.
        """
    )
# =======================
# MAIN APP
# =======================
def run_app():
    now_utc = datetime.now(timezone.utc)
    eff_bankroll = 1000.0
    eff_kelly = 0.5
    if LOGO_PATH:
        st.image(str(LOGO_PATH), width=280)
    tabs = st.tabs([
        "Fair Value Model",
        "Matchup Center",
        "Lineup Analysis",
        "Fantasy Draft",
        "Prop Research",
        "Sportsbook Screener",
        "Parlay Builder",
        "Arbitrage Tracker",
    ])
    with tabs[0]:
        fair_value_model.render(supabase, now_utc, eff_bankroll, eff_kelly, authed, debug_mode=DEBUG_MODE)
    with tabs[1]:
        matchup_center.render(supabase, now_utc, eff_bankroll, eff_kelly)
    with tabs[2]:
        lineup_comparison.render(supabase, now_utc)
    with tabs[3]:
        fantasy_draft.render()
    with tabs[4]:
        prop_leaderboard.render(supabase, now_utc)
    with tabs[5]:
        sportsbook_screener.render(supabase, now_utc)
    with tabs[6]:
        parlay_builder.render(supabase, now_utc, eff_bankroll, eff_kelly, authed)
    with tabs[7]:
        arbitrage_tracker.render(supabase, now_utc, eff_bankroll, eff_kelly)
if __name__ == "__main__":
    run_app()
