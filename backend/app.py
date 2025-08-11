import os
import requests
from fastapi import FastAPI
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import pytz

load_dotenv()
app = FastAPI()

TZ = pytz.timezone("Australia/Melbourne")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

def tg_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram env missing; skipping send.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10
        )
        print("TG send:", r.status_code, r.text)
    except Exception as e:
        print("TG send error:", e)

@app.get("/health")
def health():
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    return {"status": "ok", "time": now}

scheduler: BackgroundScheduler | None = None

def schedule_jobs():
    global scheduler
    if scheduler and scheduler.running:
        return  # prevent double-scheduling on reloads

    scheduler = BackgroundScheduler(timezone=TZ)

    # Morning check-ins
    # Weekdays (Mon–Fri) at 06:20
    scheduler.add_job(
        lambda: tg_send("🌅 Morning check-in\nHow do you feel today? (reply here)\n👍 okay / 😖 migraine / 😴 poor sleep / 🤒 other"),
        CronTrigger(day_of_week="mon-fri", hour=6, minute=20),
        id="am_weekday"
    )
    # Weekends (Sat–Sun) at 07:30
    scheduler.add_job(
        lambda: tg_send("🌅 Weekend check-in\nHow do you feel today? (reply here)\n👍 okay / 😖 migraine / 😴 poor sleep / 🤒 other"),
        CronTrigger(day_of_week="sat,sun", hour=7, minute=30),
        id="am_weekend"
    )

    # Evening migraine diary every day 20:30
    scheduler.add_job(
        lambda: tg_send("🌙 Evening diary\nHeadache today? (Y/N)\nIf yes, I’ll ask a few quick questions."),
        CronTrigger(hour=20, minute=30),
        id="pm_diary"
    )

    scheduler.start()
    tg_send("🤖 Fitness Without Migraines backend is online! (scheduler armed)")

@app.on_event("startup")
def startup_event():
    schedule_jobs()

@app.on_event("shutdown")
def shutdown_event():
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        tg_send("👋 Backend shutting down (scheduler stopped).")
