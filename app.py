# app.py
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
import httpx
import streamlit as st
import streamlit.components.v1 as components
from pathlib import Path
from PIL import Image
from cryptography.fernet import Fernet, InvalidToken
from supabase import create_client, Client, ClientOptions
from supabase_auth.errors import AuthRetryableError
from core.data_sources import infer_current_week_index
from tabs import (
    fair_value_model,
    matchup_center,
    fantasy_tools,
    prop_leaderboard,
    sportsbook_screener,
    parlay_builder,
    arbitrage_tracker,
)
# =======================
# AUTH
# =======================
# Login lives in st.session_state (survives normal reruns/tab switches
# within a browser session) plus an optional persistent browser cookie
# (survives closing/reopening the browser). A prior streamlit-cookies-
# manager-v2-based "Keep me logged in" cookie was the same bidirectional
# custom-component architecture confirmed elsewhere in this product family
# to cause a continuous reload/reconnect loop in production: a Python-
# visible return value Streamlit polls for changes, gated behind a
# readiness handshake that blocks the whole app behind st.stop() until the
# component reports back. Do not reintroduce that library or any other
# component built the same way.
#
# The persistence mechanism here is deliberately asymmetric to avoid that
# failure mode entirely:
#   - READ: st.context.cookies -- a native, read-only Streamlit property
#     populated directly from the incoming request's Cookie header. No
#     component, no async round-trip, no readiness gate, available on the
#     very first script run.
#   - WRITE: st.components.v1.html() (see _write_persisted_cookie /
#     _clear_persisted_cookie below) -- a plain iframe with a vanilla JS
#     snippet. This has no key= and no return value Streamlit tracks, so
#     it structurally cannot trigger a rerun on its own, and it's only
#     ever called when the persisted token pair actually needs to change
#     (see _persist_if_changed) -- never on a normal rerun where nothing
#     changed.
SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
COOKIE_SECRET     = os.getenv("COOKIE_SECRET", "")
if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    st.error("Auth not configured. Add SUPABASE_URL and SUPABASE_ANON_KEY to environment.")
    st.stop()

PERSIST_COOKIE_NAME = "fvb_nfl_sess"
PERSIST_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days


def _get_fernet():
    """None if COOKIE_SECRET isn't configured -- persistent login is then
    simply unavailable and the app falls back to today's session-only
    behavior; it never weakens actual Supabase auth validation."""
    if not COOKIE_SECRET:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(COOKIE_SECRET.encode()).digest())
    return Fernet(key)


def _encode_persisted_session(access_token, refresh_token):
    f = _get_fernet()
    if not f:
        return None
    payload = json.dumps({"a": access_token, "r": refresh_token}).encode()
    return f.encrypt(payload).decode()


def _decode_persisted_session(raw):
    """Encryption here only protects the plaintext token pair sitting in
    the cookie -- it is not protection against a stolen cookie being
    replayed, and not a substitute for Supabase's own token validation.
    A tampered, corrupted, or foreign value fails decryption/parsing here
    and is treated as no cookie present, never as a valid session."""
    f = _get_fernet()
    if not f or not raw:
        return None
    try:
        payload = f.decrypt(raw.encode())
        data = json.loads(payload)
        access_token, refresh_token = data.get("a"), data.get("r")
        if access_token and refresh_token:
            return access_token, refresh_token
    except (InvalidToken, ValueError, TypeError, KeyError):
        pass
    return None


def _write_persisted_cookie(value):
    # Cannot be HttpOnly -- Streamlit has no server-side response-header
    # hook to set that from application code, so the write must go through
    # JS, which is fundamentally unable to set HttpOnly cookies. Secure +
    # SameSite=Lax + a narrow Path + encrypting the payload are the
    # practical mitigations available here; see the module comment above.
    _safe_value = value.replace("'", "").replace('"', "").replace(";", "").replace("\n", "")
    components.html(
        f"<script>document.cookie = "
        f"\"{PERSIST_COOKIE_NAME}={_safe_value}; Max-Age={PERSIST_COOKIE_MAX_AGE}; "
        f"Path=/; SameSite=Lax; Secure\";</script>",
        height=0,
    )


def _clear_persisted_cookie():
    components.html(
        f"<script>document.cookie = "
        f"\"{PERSIST_COOKIE_NAME}=; Max-Age=0; Path=/; SameSite=Lax; Secure\";</script>",
        height=0,
    )


def _persist_if_changed(access_token, refresh_token):
    """Only writes the cookie when the token pair actually differs from
    what the browser is already known to hold -- a fresh login, or a
    genuine token rotation. A normal rerun where set_session() returns the
    same still-valid tokens is a no-op tuple comparison, not a cookie
    write, which is what keeps tab/filter/widget interactions from
    repeatedly rewriting the cookie."""
    _new_sig = (access_token, refresh_token)
    if st.session_state.get("_persisted_token_sig") == _new_sig:
        return
    _payload = _encode_persisted_session(access_token, refresh_token)
    if _payload is None:
        return
    _write_persisted_cookie(_payload)
    st.session_state["_persisted_token_sig"] = _new_sig
# A fresh Client (and therefore a fresh, session-less GoTrueClient) is
# created on every Streamlit rerun -- Streamlit re-executes this entire
# script top-to-bottom on every interaction (tab switch, filter change,
# button click, st.rerun()). Supabase auth state must be re-attached to
# *this rerun's* client every single time, not just once per browser
# session, otherwise any later authenticated call (e.g. auth.get_user())
# hits a client with no session and raises, aborting the script before
# run_app() ever executes -- which looks exactly like an unexpected sign-out.
#
# auto_refresh_token=False: gotrue-py's set_session() unconditionally starts
# a background threading.Timer to proactively refresh the token before it
# expires (see supabase_auth._sync.gotrue_client._save_session ->
# _start_auto_refresh_token), even when the token wasn't actually expired
# this call. Since a brand-new client is created every rerun above, that
# timer -- and the whole client object it keeps alive via its closure -- is
# never cancelled; every single rerun leaks one more live background thread.
# This is redundant here anyway: set_session() already re-validates and
# refreshes the token synchronously on every rerun (see the restore block
# below), which is the correct refresh mechanism for Streamlit's
# recreate-everything-per-rerun model. Disabling the client's own
# self-scheduled background refresh removes the leak with no loss of
# freshness or security.
supabase: Client = create_client(
    SUPABASE_URL, SUPABASE_ANON_KEY, options=ClientOptions(auto_refresh_token=False),
)
st.session_state.setdefault("sb_access_token", None)
st.session_state.setdefault("sb_refresh_token", None)
st.session_state.setdefault("sb_session", None)
st.session_state.setdefault("_persisted_token_sig", None)


def save_session(sess):
    st.session_state.sb_session = sess
    st.session_state.sb_access_token = sess.access_token
    st.session_state.sb_refresh_token = sess.refresh_token


def clear_session():
    st.session_state.sb_session = None
    st.session_state.sb_access_token = None
    st.session_state.sb_refresh_token = None
    if st.session_state.get("_persisted_token_sig") is not None:
        _clear_persisted_cookie()
        st.session_state["_persisted_token_sig"] = None


# st.session_state persists across normal reruns within this browser
# session (tab switches, filter changes, st.rerun()). If it's empty --
# a brand-new browser session, e.g. after closing and reopening the
# browser -- fall back to the persisted cookie (native read, see
# _get_fernet/_decode_persisted_session above). The cookie's own token
# pair is recorded as the known-persisted baseline *before* attempting
# set_session() below, so if set_session() returns that exact same pair
# unchanged (the common case -- token wasn't actually expired), no cookie
# write happens; a write only happens if set_session() actually rotates
# the pair (see _persist_if_changed).
_restore_access = st.session_state.get("sb_access_token")
_restore_refresh = st.session_state.get("sb_refresh_token")
if not (_restore_access and _restore_refresh):
    _cookie_tokens = _decode_persisted_session(st.context.cookies.get(PERSIST_COOKIE_NAME))
    if _cookie_tokens:
        _restore_access, _restore_refresh = _cookie_tokens
        st.session_state["_persisted_token_sig"] = _cookie_tokens

authed = False
if _restore_access and _restore_refresh:
    try:
        _res = supabase.auth.set_session(
            access_token=_restore_access,
            refresh_token=_restore_refresh,
        )
        _sess = getattr(_res, "session", None)
        if not _sess:
            raise RuntimeError("set_session returned no session")
        # set_session transparently refreshes an expired access token using
        # the refresh token -- write back whatever came out so session_state
        # stays current instead of re-refreshing on every later rerun.
        save_session(_sess)
        _persist_if_changed(_sess.access_token, _sess.refresh_token)
        authed = True
    except (AuthRetryableError, httpx.TransportError):
        # set_session() calls Supabase's /user endpoint synchronously on
        # EVERY rerun (even when the access token is still valid, not just
        # when refreshing) to validate it -- see gotrue-py's set_session,
        # the "not expired" branch calls self.get_user(access_token). A
        # transient failure of that single network call (a timeout, a
        # brief 5xx/gateway error, a DNS/connection hiccup -- gotrue-py
        # itself classifies these as AuthRetryableError; raw connection/
        # timeout errors aren't even wrapped and surface as httpx errors)
        # is NOT proof the stored token is actually invalid. Leave the
        # stored tokens (and the persisted cookie) alone here; this rerun
        # just renders as signed-out, and the very next interaction
        # retries set_session() with the same tokens, self-healing without
        # forcing a re-login or destroying an otherwise-valid cookie.
        authed = False
    except Exception:
        # A genuine rejection -- the tokens are actually invalid or expired
        # past refresh, or a real (non-transient) error occurred. Fail
        # closed: clear the stale session state AND the persisted cookie
        # (clear_session does both) rather than leaving authed=True paired
        # with a client that has no session. Same end state as an
        # explicit logout.
        clear_session()
        authed = False
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
        # authed already means a session was successfully attached to this
        # rerun's client above -- this is defense-in-depth against a
        # transient failure here (e.g. a network blip), not the session
        # restoration itself. A failure here should not sign the user out;
        # it just means the email can't be shown this rerun.
        try:
            user = supabase.auth.get_user()
            user_email = (
                getattr(user.user, "email", None)
                if user and getattr(user, "user", None)
                else None
            )
        except Exception:
            user_email = None
        st.success(f"Signed in{f' as {user_email}' if user_email else ''}.")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Log out", use_container_width=True):
                try:
                    supabase.auth.sign_out()
                except Exception:
                    pass
                _clear_persisted_cookie()
                st.session_state.clear()
                st.rerun()
    else:
        st.info(
            "Free full access in September — create a free account to unlock filters and sorting."
        )
        with st.form("login_form_sidebar", clear_on_submit=False, border=True):
            email       = st.text_input("Email", key="signin_email_sidebar")
            password    = st.text_input("Password", type="password", key="signin_pw_sidebar")
            submit      = st.form_submit_button("Sign in", use_container_width=True)
        if submit:
            try:
                res  = supabase.auth.sign_in_with_password(
                    {"email": (email or "").strip(), "password": password}
                )
                sess = getattr(res, "session", None)
                if sess:
                    save_session(sess)
                    _persist_if_changed(sess.access_token, sess.refresh_token)
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
3. **Fantasy Tools** — Lineup Analysis (research weekly usage, props, game
   environment, and matchup context for one player, or compare up to four) and
   Draft Rankings (consensus ADP across platforms, filterable by position).
4. **Prop Research** — research an individual player's props, season stats, recent
   games, and matchup context directly, or find the top players on the best current
   hit-rate streak for a selected prop.
5. **Sportsbook Screener** — pure line shopping across every sportsbook.
6. **Parlay Builder** — build and compare multi-leg parlays across sportsbooks.
7. **Arbitrage Tracker** — scan current prices for markets where the best price on
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
        "Fantasy Tools",
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
        fantasy_tools.render(supabase, now_utc)
    with tabs[3]:
        prop_leaderboard.render(supabase, now_utc)
    with tabs[4]:
        sportsbook_screener.render(supabase, now_utc)
    with tabs[5]:
        parlay_builder.render(supabase, now_utc, eff_bankroll, eff_kelly, authed)
    with tabs[6]:
        arbitrage_tracker.render(supabase, now_utc, eff_bankroll, eff_kelly)
if __name__ == "__main__":
    run_app()
