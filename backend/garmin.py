import os
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple, Callable
from contextlib import suppress

from sqlmodel import Session, select
from dotenv import load_dotenv
load_dotenv()

from collections import defaultdict
from datetime import datetime

from garminconnect import Garmin  # type: ignore

from .models import MetricsCache, engine, create_db_and_tables, HeartRateHourly

from zoneinfo import ZoneInfo
from sqlmodel import select, delete  # delete is new if you want bulk-delete
import statistics as stats

EMAIL = os.getenv("GARMIN_EMAIL")
PASSWORD = os.getenv("GARMIN_PASSWORD")

# ---- helpers -----------------------------------------------------------------

def _first_method(obj: Any, names: List[str]) -> Optional[Callable]:
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            return fn
    return None

def _login() -> Garmin:
    if not EMAIL or not PASSWORD:
        raise RuntimeError("GARMIN_EMAIL or GARMIN_PASSWORD not set")
    g = Garmin(EMAIL, PASSWORD)
    g.login()
    return g

def _upsert_metrics(session: Session, day: dt.date, payload: Dict[str, Any]) -> Dict[str, Any]:
    # Prefer your existing helper if present
    with suppress(Exception):
        q = MetricsCache.select_one(source="GARMIN", the_day=day)  # type: ignore[attr-defined]
        row = session.exec(q)
        if row is not None:
            row.payload = payload
            session.add(row)
            session.commit()
            return payload

    # Fallback: do a standard upsert by query
    existing = session.exec(
        select(MetricsCache).where(
            MetricsCache.source == "GARMIN",
            MetricsCache.the_day == day
        )
    ).first()
    if existing:
        existing.payload = payload
        session.add(existing)
    else:
        session.add(MetricsCache(source="GARMIN", the_day=day, payload=payload))
    session.commit()
    return payload

def _normalize_sleep(raw: Any) -> Dict[str, Any]:
    """
    Normalize various shapes Garmin returns for sleep.
    We try to surface: total_minutes_asleep, efficiency, stages, bedtime, wakeups
    """
    if raw is None:
        return {"sleep": None}

    # Common patterns seen in garminconnect outputs
    total = None
    efficiency = None
    bedtime = None
    wakeups = None
    stages = None

    # Try common keys
    if isinstance(raw, dict):
        # total duration
        total = (
            raw.get("totalSleepSeconds")
            or raw.get("sleepingSeconds")
            or raw.get("overallSleepSeconds")
        )
        if total is not None:
            total = int(total) // 60  # minutes
        efficiency = raw.get("sleepEfficiency")
        wakeups = raw.get("awakeningsCount") or raw.get("wakeupCount")
        bedtime = raw.get("sleepTime") or raw.get("startTimeGMT") or raw.get("startTimeLocal")

        # stages can come as nested dicts or arrays
        stages = raw.get("sleepLevels") or raw.get("sleepStages") or raw.get("levels")

    # If list (some versions return array per day)
    if isinstance(raw, list) and raw:
        # Pick the first element (assume single day)
        return _normalize_sleep(raw[0])

    return {
        "sleep": {
            "total_minutes_asleep": total,
            "efficiency": efficiency,
            "stages": stages,
            "bedtime": bedtime,
            "wakeups": wakeups,
            "raw": raw,
        }
    }

def _normalize_hr(raw: Any) -> Dict[str, Any]:
    """
    Normalize daily heart rate series.
    We try to surface: series [{timestamp, bpm}], resting_hr if present.
    """
    if raw is None:
        return {"heart_rate": None}

    series: List[Dict[str, Any]] = []
    resting = None

    if isinstance(raw, dict):
        # Many versions: { "heartRateValues": [[ts, bpm], ...], "restingHeartRate": 54, ... }
        arr = raw.get("heartRateValues") or raw.get("heartRateValuesV2") or raw.get("values")
        resting = raw.get("restingHeartRate") or raw.get("restingHR")
        if isinstance(arr, list):
            for pair in arr:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    ts, bpm = pair[0], pair[1]
                    series.append({"timestamp": ts, "bpm": bpm})
        # Some versions: already parsed as objects
        elif isinstance(arr, dict) and "samples" in arr:
            for s in arr["samples"]:
                series.append({"timestamp": s.get("time"), "bpm": s.get("bpm")})

    return {"heart_rate": {"series": series, "resting_hr": resting, "raw": raw}}

MELB = ZoneInfo("Australia/Melbourne")

def _parse_ts(ts) -> Optional[dt.datetime]:
    # Accept epoch ms/sec or ISO strings
    try:
        if isinstance(ts, (int, float)):
            # Heuristic: ms vs sec
            if ts > 1_000_000_000_000:  # ms
                return dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc)
            else:  # sec
                return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        if isinstance(ts, str):
            # tolerate trailing 'Z' or fractional seconds
            t = ts.replace("Z", "+00:00")
            return dt.datetime.fromisoformat(t)
    except Exception:
        return None
    return None

def _upsert_hr_hourly(the_day: dt.date, payload: Dict[str, Any]) -> int:
    """
    Aggregate heart_rate.series into hourly rows (local Melbourne time) and upsert.
    Returns number of hours written.
    """
    series = ((payload or {}).get("heart_rate") or {}).get("series") or []
    buckets: Dict[int, List[int]] = {}

    for s in series:
        bpm = s.get("bpm") or s.get("value")
        ts  = s.get("timestamp") or s.get("ts")
        if not isinstance(bpm, (int, float)) or ts is None:
            continue
        t = _parse_ts(ts)
        if not isinstance(t, dt.datetime):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        local = t.astimezone(MELB)
        if local.date() != the_day:
            # keep only samples that fall on this local day
            continue
        buckets.setdefault(local.hour, []).append(int(bpm))

    rows = []
    for hour, vals in buckets.items():
        hr_mean = round(float(stats.fmean(vals)), 1)
        hr_min  = int(min(vals))
        hr_max  = int(max(vals))
        rows.append(HeartRateHourly(
            the_day=the_day, hour=hour,
            hr_mean=hr_mean, hr_min=hr_min, hr_max=hr_max, samples=len(vals)
        ))

    with Session(engine) as s:
        # delete existing hour bins for that day, then insert fresh
        s.exec(delete(HeartRateHourly).where(HeartRateHourly.the_day == the_day))
        for r in rows:
            s.add(r)
        s.commit()

    return len(rows)



# ---- public API --------------------------------------------------------------

def fetch_and_store(day: dt.date) -> Dict[str, Any]:
    """
    Log in, fetch sleep + HR for a single day with method fallbacks,
    store normalized payload in MetricsCache, then upsert hourly HR bins.
    """
    create_db_and_tables()
    g = _login()
    try:
        # --- Sleep (try multiple method names) ---
        sleep_fn = _first_method(g, [
            "get_sleep_data",
            "get_sleep",
            "get_sleep_data_by_date",
        ])
        sleep_raw = None
        if sleep_fn:
            with suppress(Exception):
                sleep_raw = sleep_fn(day.strftime("%Y-%m-%d"))

        # --- Heart rate (try multiple method names) ---
        hr_fn = _first_method(g, [
            "get_heart_rates",
            "get_daily_heart_rate",
            "get_daily_hr",
            "get_heart_rate",
        ])
        hr_raw = None
        if hr_fn:
            with suppress(Exception):
                hr_raw = hr_fn(day.strftime("%Y-%m-%d"))

        # --- Normalize & store raw day payload ---
        payload = {"date": day.isoformat()}
        payload.update(_normalize_sleep(sleep_raw))
        payload.update(_normalize_hr(hr_raw))

        with Session(engine) as s:
            saved = _upsert_metrics(s, day, payload)

        # --- Build hourly HR bins from normalized payload (local day) ---
        try:
            hours_written = _upsert_hr_hourly(day, payload)  # writes HeartRateHourly rows
            saved["hourly_bins_written"] = hours_written
        except Exception as e:
            saved["hourly_bins_error"] = str(e)

        return saved

    finally:
        with suppress(Exception):
            g.logout()

def fetch_and_store_yesterday() -> Dict[str, Any]:
    return fetch_and_store(dt.date.today() - dt.timedelta(days=1))

def fetch_and_store_today() -> Dict[str, Any]:
    return fetch_and_store(dt.date.today())

# --- compatibility aliases expected by app.py ---
from typing import Optional
from sqlmodel import Session as SQLSession  # type: ignore

def fetch_sleep_and_hr_for(day: dt.date, s: Optional[SQLSession] = None):
    # session 's' is optional and unused; fetch_and_store handles its own session
    return fetch_and_store(day)

def fetch_sleep_and_hr_for_yesterday(s: Optional[SQLSession] = None):
    return fetch_and_store_yesterday()

def fetch_sleep_and_hr_for_today(s: Optional[SQLSession] = None):
    return fetch_and_store_today()

