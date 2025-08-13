import os
import time
import datetime as dt
from datetime import datetime, timedelta, date
from typing import Optional

import requests
from fastapi import FastAPI, Request, Query
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlmodel import Session, select
from zoneinfo import ZoneInfo

from .garmin import fetch_sleep_and_hr_for
from .models import (
    create_db_and_tables,
    engine,
    TelegramMessage,
    Direction,  # Enum: IN / OUT
    get_session,
    MetricsCache,
    HeartRateHourly,
)
from .strava import (
    auth_start_url,
    exchange_code_for_token,
    fetch_activities_since,
    fetch_activities_after,
    _latest_strava_start_ts,
)

from openai import OpenAI
from .ai import build_health_context, build_prompt

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI()  # uses env var

# --- Config ---
load_dotenv()
app = FastAPI()
MELB = ZoneInfo("Australia/Melbourne")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

scheduler: Optional[BackgroundScheduler] = None

# -------- Telegram helpers --------
def tg_send(text: str, chat_id: Optional[str] = None):
    """Send a Telegram message and log it."""
    cid = chat_id or CHAT_ID
    if not BOT_TOKEN or not cid:
        print("Telegram env missing; skip send.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": text},
            timeout=10,
        )
        with Session(engine) as s:
            s.add(TelegramMessage(direction=Direction.OUT, chat_id=str(cid), text=text))
            s.commit()
        print("TG send:", r.status_code, r.text)
    except Exception as e:
        print("TG send error:", e)

# -------- Health --------
@app.get("/health")
def health():
    now = datetime.now(MELB).strftime("%Y-%m-%d %H:%M:%S %Z")
    return {"status": "ok", "time": now}

# -------- Scheduling --------
def _nightly_strava():
    """Incremental Strava sync once per night."""
    try:
        with Session(engine) as s:
            last_ts = _latest_strava_start_ts(s)
            if last_ts is None:
                fetch_activities_since(s, days=7)  # small backfill if empty
            else:
                fetch_activities_after(s, after_ts=last_ts + 1)
        tg_send("🕑 Nightly Strava sync OK.")
    except Exception as e:
        tg_send(f"❌ Nightly Strava sync failed: {e}")

def _garmin_nightly():
    """Pull yesterday's sleep + HR from Garmin each morning (local day)."""
    try:
        with Session(engine) as s:
            local_today = datetime.now(MELB).date()
            y = local_today - timedelta(days=1)
            fetch_sleep_and_hr_for(y, s)
        tg_send("🕑 Nightly Garmin sync OK (sleep + HR).")
    except Exception as e:
        tg_send(f"❌ Nightly Garmin sync failed: {e}")

def schedule_jobs():
    """Set up AM/PM prompts and nightly syncs (idempotent)."""
    global scheduler
    if scheduler and scheduler.running:
        return

    scheduler = BackgroundScheduler(timezone=MELB)

    # Weekdays 06:20
    scheduler.add_job(
        lambda: tg_send("🌅 Morning check-in\nHow do you feel today? (ok / migraine / poor sleep / other)"),
        CronTrigger(day_of_week="mon-fri", hour=6, minute=20),
        id="am_weekday",
        replace_existing=True,
    )

    # Weekends 07:30
    scheduler.add_job(
        lambda: tg_send("🌅 Weekend check-in\nHow do you feel today? (ok / migraine / poor sleep / other)"),
        CronTrigger(day_of_week="sat,sun", hour=7, minute=30),
        id="am_weekend",
        replace_existing=True,
    )

    # Daily 20:30 diary
    scheduler.add_job(
        lambda: tg_send("🌙 Evening diary\nHeadache today? (yes / no)"),
        CronTrigger(hour=20, minute=30),
        id="pm_diary",
        replace_existing=True,
    )

    # Nightly incremental Strava sync at 02:15
    scheduler.add_job(
        _nightly_strava,
        CronTrigger(hour=2, minute=15),
        id="strava_nightly_sync",
        replace_existing=True,
    )

    # Morning Garmin sync at 06:10 (pulls yesterday's sleep/HR)
    scheduler.add_job(
        _garmin_nightly,
        CronTrigger(hour=6, minute=10),
        id="garmin_morning_sync",
        replace_existing=True,
    )

    scheduler.start()
    tg_send("🤖 Backend online — scheduler armed.")

@app.get("/debug/summary/garmin/hourly")
def debug_garmin_hourly(day: str):
    the_day = date.fromisoformat(day)
    with get_session() as s:
        rows = s.exec(
            select(HeartRateHourly)
            .where(HeartRateHourly.the_day == the_day)
            .order_by(HeartRateHourly.hour)
        ).all()
    return [r.dict() for r in rows]


@app.on_event("startup")
def startup_event():
    create_db_and_tables()
    schedule_jobs()

@app.on_event("shutdown")
def shutdown_event():
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        tg_send("👋 Backend shutting down (scheduler stopped).")

def _schedule_2h_followup(chat_id: str):
    """Ask RPE 2 hours after a workout starts (local time)."""
    when = datetime.now(MELB) + timedelta(hours=2)
    if scheduler:
        scheduler.add_job(
            lambda: tg_send("⏱️ How did the workout feel? (RPE 1–10)"),
            DateTrigger(run_date=when),
        )
    tg_send(f"💡 Got it. I’ll check in again at {when.strftime('%H:%M')} for RPE.", chat_id)

# -------- Telegram webhook --------
@app.post("/webhook/telegram")
async def tg_webhook(req: Request):
    data = await req.json()
    try:
        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()
        with Session(engine) as s:
            s.add(TelegramMessage(direction=Direction.IN, chat_id=chat_id, text=text))
            s.commit()
    except Exception as e:
        print("TG webhook parse error:", e)
        return {"ok": True}

    low = text.lower()
    if low in {"ok", "okay", "ready"}:
        tg_send("👍 Noted. (Next: I’ll pull sleep & training data before recommending Zwift or rest.)", chat_id)
        _schedule_2h_followup(chat_id)
    elif "migraine" in low:
        tg_send("😖 Sorry you’re migraine-y today. I’ll default to rest & recovery prompts.", chat_id)
    elif "poor sleep" in low or "bad sleep" in low:
        tg_send("😴 Thanks. I’ll be cautious with intensity today.", chat_id)
        _schedule_2h_followup(chat_id)
    elif low in {"yes", "y"}:
        tg_send("📝 Thanks — logging ‘headache today’. I’ll ask for details soon.", chat_id)
    elif low in {"no", "n"}:
        tg_send("🙌 Great — no headache logged today.", chat_id)
    elif low.startswith("rpe"):
        tg_send("✅ RPE noted. (Will incorporate into tomorrow’s plan.)", chat_id)
    else:
        tg_send("🤖 For AM: ok / migraine / poor sleep. For PM: yes / no. You can also send 'RPE 6'.", chat_id)

    return {"ok": True}

# -------- Strava OAuth & sync --------
@app.get("/auth/strava/start")
def strava_start():
    return {"auth_url": auth_start_url()}

@app.get("/auth/strava/callback")
def strava_callback(code: str):
    try:
        tok = exchange_code_for_token(code)
        access_token = tok.get("access_token")
        refresh_token = tok.get("refresh_token", "")
        expires_at = tok.get("expires_at") or (int(time.time()) + int(tok.get("expires_in", 3600)))
        if not access_token:
            raise RuntimeError(f"Missing access_token in Strava response: {tok}")

        from .models import OAuthToken, Provider  # lazy import to avoid cycles
        with Session(engine) as s:
            row = s.exec(select(OAuthToken).where(OAuthToken.provider == Provider.STRAVA)).first()
            if not row:
                row = OAuthToken(
                    provider=Provider.STRAVA,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=expires_at,
                )
            else:
                row.access_token = access_token
                row.refresh_token = refresh_token or row.refresh_token
                row.expires_at = expires_at
            s.add(row)
            s.commit()
        tg_send("✅ Strava connected.")
        return {"ok": True}
    except Exception as e:
        tg_send(f"❌ Strava auth failed: {e}")
        return {"ok": False, "error": str(e)}

@app.post("/strava/sync")
def strava_sync(days: int = 60):
    try:
        with Session(engine) as s:
            n = fetch_activities_since(s, days=days)
        tg_send(f"📥 Strava sync done. Imported {n} activities (last {days} days).")
        return {"imported": n}
    except Exception as e:
        tg_send(f"❌ Strava sync failed: {e}")
        return {"ok": False, "error": str(e)}

@app.post("/strava/sync_incremental")
def strava_sync_incremental(default_backfill_days: int = 7):
    try:
        with Session(engine) as s:
            last_ts = _latest_strava_start_ts(s)
            if last_ts is None:
                n = fetch_activities_since(s, days=default_backfill_days)
            else:
                n = fetch_activities_after(s, after_ts=last_ts + 1)
        tg_send(f"📥 Strava incremental sync done. Imported {n} activities.")
        return {"imported": n}
    except Exception as e:
        tg_send(f"❌ Strava incremental sync failed: {e}")
        return {"ok": False, "error": str(e)}

# -------- Garmin quick-sync routes (local day aware) --------
@app.post("/garmin/sync_today")
def garmin_sync_today():
    with Session(engine) as s:
        local_today = datetime.now(MELB).date()
        return fetch_sleep_and_hr_for(local_today, s)

@app.post("/garmin/sync_yesterday")
def garmin_sync_yesterday():
    with Session(engine) as s:
        local_today = datetime.now(MELB).date()
        y = local_today - timedelta(days=1)
        data = fetch_sleep_and_hr_for(y, s)
        tg_send("📥 Garmin sync OK (sleep + HR for yesterday).")
        return {"ok": True, "day": str(y), "keys": list(data.keys())}

# -------- Debug: view stored metrics --------
@app.get("/debug/metrics")
def debug_metrics(
    limit: int = Query(20, ge=1, le=200),
    source: Optional[str] = Query(None, description="e.g., GARMIN or STRAVA"),
    since: Optional[str] = Query(None, description="YYYY-MM-DD (inclusive)"),
    until: Optional[str] = Query(None, description="YYYY-MM-DD (inclusive)"),
):
    """
    Lists recent MetricsCache rows with optional filters.
    Returns: [{the_day, source, payload}]
    """
    from datetime import date

    def _parse(d: Optional[str]) -> Optional[date]:
        if not d:
            return None
        return date.fromisoformat(d)

    q = select(MetricsCache)
    if source:
        q = q.where(MetricsCache.source == source)
    s_since = _parse(since)
    s_until = _parse(until)
    if s_since:
        q = q.where(MetricsCache.the_day >= s_since)
    if s_until:
        q = q.where(MetricsCache.the_day <= s_until)
    q = q.order_by(MetricsCache.the_day.desc()).limit(limit)

    with get_session() as s:
        rows = s.exec(q).all()

    out = []
    for r in rows:
        out.append({
            "the_day": r.the_day.isoformat(),
            "source": r.source,
            "sleep_total_minutes": (r.payload or {}).get("sleep", {}).get("total_minutes_asleep"),
            "resting_hr": (r.payload or {}).get("heart_rate", {}).get("resting_hr"),
            "payload": r.payload,
        })
    return out

@app.post("/ai/recommend")
def ai_recommend(days: int = 14):
    ctx = build_health_context(days=days)
    prompt = build_prompt(ctx)
    # Chat Completions (stable + simple)
    resp = client.chat.completions.create(
        model="gpt-5",
        messages=[
            {"role":"system","content":"You are a precise endurance coach. Output under 120 words."},
            {"role":"user","content": prompt},
        ],
        temperature=0.2,
    )
    text = resp.choices[0].message.content
    # (Optional) also Telegram it:
    tg_send(f"🏁 Plan:\n{text}")
    return {"plan": text, "chars": len(text)}

# --- GPT Ask feature: step 1 ---
from pydantic import BaseModel
from fastapi import Request
import json as _json
import hashlib as _hashlib

class AskRequest(BaseModel):
    message: str
    days: int = 7

# Middleware: detect '@gpt' and expose flags on request.state
@app.middleware("http")
async def _detect_gpt_tag(request: Request, call_next):
    request.state.is_gpt = False
    request.state.gpt_message = None
    if request.method.upper() == "POST":
        try:
            body = await request.body()
            # Ensure downstream can read body again
            request._body = body
            data = _json.loads(body.decode() or "{}")
            msg = data.get("message")
            if isinstance(msg, str) and msg.lstrip().lower().startswith("@gpt"):
                request.state.is_gpt = True
                # strip the '@gpt' prefix for downstream usage
                request.state.gpt_message = msg.lstrip()[4:].strip()
        except Exception:
            # Non-JSON or missing body; ignore
            pass
    return await call_next(request)


@app.post("/ask_gpt")
async def ask_gpt(req: AskRequest):
    # Step 2: build real context from DB
    question = req.message
    if isinstance(question, str) and question.lstrip().lower().startswith("@gpt"):
        question = question.lstrip()[4:].strip()

    # Build compact context
    ctx = build_health_context(days=req.days)
    ctx_hash = _hashlib.sha256(_json.dumps(ctx, sort_keys=True).encode()).hexdigest()[:12]

    # For now, return the context + echo; Step 3 will call GPT
    summary_note = (
        f"Context built for last {req.days} day(s). "
        f"Daily entries: {len(ctx.get('daily', []))}; "
        f"Hourly rows (latest day): {len(ctx.get('hourly', []))}."
    )
    answer = f"[stub] You asked: {question}. I have included the recent context; next step will query GPT."
    return {"answer": answer, "used_context": {"hash": ctx_hash, "time_range_days": req.days, "note": summary_note}, "context": ctx}


# Optional chat entrypoint that routes to /ask_gpt if '@gpt' was detected by middleware
@app.post("/chat")
async def chat(req: AskRequest, request: Request):
    if getattr(request.state, "is_gpt", False):
        return await ask_gpt(req)
    return {"status": "ok", "routed": "chat", "echo": req.message}
