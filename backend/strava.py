import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from sqlmodel import Session, select

from .models import OAuthToken, Provider, MetricsCache, Source

# --- Config from environment ---
CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
SCOPES = os.getenv("STRAVA_SCOPES", "read,activity:read_all")
REDIRECT_BASE = os.getenv("STRAVA_REDIRECT_BASE", "")
REDIRECT_URI = f"{REDIRECT_BASE}/auth/strava/callback"

# --- Strava Endpoints ---
AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"


def auth_start_url() -> str:
    """
    Build the Strava OAuth URL. We force approval so updated scopes are applied.
    """
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "force",
        "scope": SCOPES,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code_for_token(code: str) -> dict:
    """
    Exchange the OAuth 'code' for access/refresh tokens.
    """
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    }
    r = requests.post(TOKEN_URL, data=data, timeout=20)
    r.raise_for_status()
    return r.json()


def _normalize_token_fields(j: dict) -> tuple[str, str, int]:
    """
    Return (access_token, refresh_token, expires_at) with sensible fallbacks.
    Strava sometimes returns expires_in instead of expires_at.
    """
    access = j.get("access_token")
    if not access:
        raise RuntimeError(f"Missing access_token in Strava response: {j}")

    refresh = j.get("refresh_token", "") or ""
    expires_at = j.get("expires_at")
    if not expires_at:
        expires_at = int(time.time()) + int(j.get("expires_in", 3600))
    return access, refresh, int(expires_at)


def refresh_token_if_needed(session: Session) -> str:
    """
    Ensure we have a valid access token; refresh if expired/missing expiry.
    """
    row = session.exec(select(OAuthToken).where(OAuthToken.provider == Provider.STRAVA)).first()
    if not row:
        raise RuntimeError("No Strava token stored")

    now = int(time.time())
    # If expires_at exists and is in the future, reuse current token.
    if row.expires_at and row.expires_at > now + 60:
        return row.access_token

    # Otherwise refresh
    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": row.refresh_token or "",
        },
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    access, refresh, expires_at = _normalize_token_fields(j)

    row.access_token = access
    row.refresh_token = refresh or row.refresh_token or ""
    row.expires_at = expires_at
    session.add(row)
    session.commit()
    return row.access_token


def fetch_activities_since(session: Session, days: int = 60) -> int:
    """
    Pull activities since N days ago and store each as a MetricsCache row
    (source=STRAVA, the_day=<local start date>, payload=<raw activity JSON>).
    Returns number of activities saved.
    """
    access_token = refresh_token_if_needed(session)
    after = int((datetime.utcnow() - timedelta(days=days)).timestamp())
    page, per_page = 1, 100
    saved = 0

    while True:
        r = requests.get(
            ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"after": after, "page": page, "per_page": per_page},
            timeout=30,
        )
        r.raise_for_status()
        items = r.json()
        if not items:
            break

        for a in items:
            # Use local start time if present, else UTC; normalize to date()
            start_local = a.get("start_date_local") or a.get("start_date")
            # Ensure ISO format with timezone; Strava often returns Z-terminated strings
            dt = datetime.fromisoformat(start_local.replace("Z", "+00:00"))
            day = dt.date()

            mc = MetricsCache(source=Source.STRAVA, the_day=day, payload=a)
            session.add(mc)
            saved += 1

        session.commit()
        page += 1

    return saved


# ADD near the bottom of strava.py
def _latest_strava_start_ts(session: Session) -> int | None:
    """Return unix ts of the most recent stored Strava activity (from payload.start_date), or None."""
    from .models import MetricsCache, Source
    # get most recent day
    row = session.exec(
        select(MetricsCache).where(MetricsCache.source == Source.STRAVA)
        .order_by(MetricsCache.the_day.desc())
        .limit(1)
    ).first()
    if not row:
        return None
    try:
        start = (row.payload.get("start_date") or row.payload.get("start_date_local"))
        if not start:
            return None
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None

def fetch_activities_after(session: Session, after_ts: int) -> int:
    """Like fetch_activities_since, but uses an explicit 'after' unix timestamp."""
    access_token = refresh_token_if_needed(session)
    page, per_page = 1, 100
    saved = 0
    while True:
        r = requests.get(
            ACTIVITIES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"after": after_ts, "page": page, "per_page": per_page},
            timeout=30,
        )
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        for a in items:
            start_local = a.get("start_date_local") or a.get("start_date")
            dt = datetime.fromisoformat(start_local.replace("Z", "+00:00"))
            day = dt.date()
            session.add(MetricsCache(source=Source.STRAVA, the_day=day, payload=a))
            saved += 1
        session.commit()
        page += 1
    return saved
