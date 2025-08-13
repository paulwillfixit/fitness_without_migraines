# backend/ai.py
from typing import Dict
from sqlmodel import select
from .models import get_session, DailyHealthSummary, HeartRateHourly

def build_health_context(days: int = 14) -> Dict:
    """
    Returns a compact context dict:
      - "daily": last N complete DailyHealthSummary rows (oldest → newest)
      - "hourly": hourly HR means for the most recent day we have (often 'today')
      - "today_partial": interim aggregates if the latest day is 'today'
    """
    with get_session() as s:
        # Completed daily summaries
        rows = s.exec(
            select(DailyHealthSummary)
            .order_by(DailyHealthSummary.the_day.desc())
            .limit(days)
        ).all()
        daily_out = []
        for r in reversed(rows):  # oldest → newest
            daily_out.append({
                "day": r.the_day.isoformat(),
                "sleep_min": r.sleep_minutes,
                "sleep_eff": r.sleep_efficiency,
                "sleep_score": r.sleep_score,
                "resting_hr": r.resting_hr,
                "hr_mean": r.hr_mean,
                "hr_min": r.hr_min,
                "hr_max": r.hr_max,
                "steps": getattr(r, "steps", None),
                "tss": getattr(r, "tss", None),
            })

        # Most recent day with hourly HR
        latest_day = None
        last = s.exec(
            select(HeartRateHourly)
            .order_by(HeartRateHourly.the_day.desc(), HeartRateHourly.hour.desc())
            .limit(1)
        ).all()
        if last:
            latest_day = last[0].the_day

        hourly = []
        today_partial = None
        if latest_day:
            hours = s.exec(
                select(HeartRateHourly)
                .where(HeartRateHourly.the_day == latest_day)
                .order_by(HeartRateHourly.hour)
            ).all()

            # Compact hourly means
            hourly = [
                {"h": h.hour, "m": round(float(h.hr_mean), 1) if h.hr_mean is not None else None}
                for h in hours
            ]

            # Interim aggregates if latest_day is today (date-only check)
            from datetime import datetime
            if latest_day == datetime.now().date():
                valid_means = [float(h.hr_mean) for h in hours if h.hr_mean is not None]
                mins = [int(h.hr_min) for h in hours if h.hr_min is not None]
                maxs = [int(h.hr_max) for h in hours if h.hr_max is not None]
                samples = [int(h.samples) for h in hours if h.samples is not None]

                mean_so_far = round(sum(valid_means) / len(valid_means), 1) if valid_means else None
                # simple median without numpy
                median_so_far = None
                if valid_means:
                    v = sorted(valid_means)
                    mid = len(v) // 2
                    median_so_far = round((v[mid] if len(v) % 2 == 1 else (v[mid - 1] + v[mid]) / 2), 1)

                min_so_far = min(mins) if mins else None
                max_so_far = max(maxs) if maxs else None
                samples_total = sum(samples) if samples else 0

                today_partial = {
                    "day": latest_day.isoformat(),
                    "partial": True,
                    "hr_mean_so_far": mean_so_far,
                    "hr_median_so_far": median_so_far,
                    "hr_min_so_far": min_so_far,
                    "hr_max_so_far": max_so_far,
                    "hours_observed": len(hours),
                    "samples_total": samples_total,
                }

        return {"daily": daily_out, "hourly": hourly, "today_partial": today_partial}

def build_prompt(ctx: Dict) -> str:
    """
    Compact, GPT-friendly prompt. Includes note to weigh today's partial HR if present.
    """
    return (
        "You are a coach aware of migraines. Use the recent daily summary and the most "
        "recent day's hourly HR. If 'today_partial' exists, weigh it for near-term advice. "
        "Recommend tonight's plan (Zwift/rest/intensity) in ≤3 bullets. Be conservative if "
        "sleep or resting HR trends worsen.\n"
        f"DATA:\n{ctx}"
    )
