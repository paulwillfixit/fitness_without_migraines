import os
import requests
from datetime import datetime, timedelta

import pytz
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlmodel import Session

from .models import (
    create_db_and_tables,
    engine,
    TelegramMessage,
    Direction,  # Enum: IN / OUT
)

# --- Config & globals ---
load_dotenv()
app = FastAPI()

TZ = pytz.timezone("Australia/Melbourne")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

scheduler: BackgroundScheduler | None = None


# --- Telegram helpers ---
def tg_send(text: str, chat_id: str | None = None):
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
        # log outbound
        with Session(engine) as s:
            s.add(TelegramMessage(direction=Direction.OUT, chat_id=str(cid), text=text))
            s.commit()
        print("TG send:", r.status_code, r.text)
    except Exception as e:
        print("TG send error:", e)


# --- Health ---
@app.get("/health")
def health():
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    return {"status": "ok", "time": now}


# --- Scheduling ---
def schedule_jobs():
    global scheduler
    if scheduler and scheduler.running:
        return

    scheduler = BackgroundScheduler(timezone=TZ)

    # Weekdays 06:20
    scheduler.add_job(
        lambda: tg_send(
            "🌅 Morning check-in\nHow do you feel today? (ok / migraine / poor sleep / other)"
        ),
        CronTrigger(day_of_week="mon-fri", hour=6, minute=20),
        id="am_weekday",
        replace_existing=True,
    )

    # Weekends 07:30
    scheduler.add_job(
        lambda: tg_send(
            "🌅 Weekend check-in\nHow do you feel today? (ok / migraine / poor sleep / other)"
        ),
        CronTrigger(day_of_week="sat,sun", hour=7, minute=30),
        id="am_weekend",
        replace_existing=True,
    )

    # Daily 20:30
    scheduler.add_job(
        lambda: tg_send("🌙 Evening diary\nHeadache today? (yes / no)"),
        CronTrigger(hour=20, minute=30),
        id="pm_diary",
        replace_existing=True,
    )

    scheduler.start()
    tg_send("🤖 Backend online — scheduler armed.")


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
    """Ask RPE 2 hours after a workout starts."""
    when = datetime.now(TZ) + timedelta(hours=2)
    if scheduler:
        scheduler.add_job(
            lambda: tg_send("⏱️ How did the workout feel? (RPE 1–10)"),
            DateTrigger(run_date=when),
        )
    tg_send(f"💡 Got it. I’ll check in again at {when.strftime('%H:%M')} for RPE.", chat_id)


# --- Telegram webhook ---
@app.post("/webhook/telegram")
async def tg_webhook(req: Request):
    data = await req.json()
    try:
        msg = data["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()
        # log inbound
        with Session(engine) as s:
            s.add(TelegramMessage(direction=Direction.IN, chat_id=chat_id, text=text))
            s.commit()
    except Exception as e:
        print("TG webhook parse error:", e)
        return {"ok": True}

    low = text.lower()
    if low in {"ok", "okay", "ready"}:
        tg_send(
            "👍 Noted. (Next: I’ll pull sleep & training data before recommending Zwift or rest.)",
            chat_id,
        )
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
        tg_send(
            "🤖 For AM use: ok / migraine / poor sleep. For PM: yes / no. You can also send 'RPE 6'.",
            chat_id,
        )

    return {"ok": True}
