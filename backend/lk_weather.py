"""Weather for all 25 Sri Lankan districts in ONE Open-Meteo call (keyless), plus a once-a-day briefing."""
import json
import os
from datetime import date, datetime

import requests

from weather_service import OPEN_METEO_URL, UA, WMO

# District capital coordinates.
DISTRICTS = [
    ("Colombo", 6.9271, 79.8612), ("Gampaha", 7.0873, 79.9990), ("Kalutara", 6.5854, 79.9607),
    ("Kandy", 7.2906, 80.6337), ("Matale", 7.4675, 80.6234), ("Nuwara Eliya", 6.9497, 80.7891),
    ("Galle", 6.0535, 80.2210), ("Matara", 5.9549, 80.5550), ("Hambantota", 6.1241, 81.1185),
    ("Jaffna", 9.6615, 80.0255), ("Kilinochchi", 9.3803, 80.3770), ("Mannar", 8.9810, 79.9044),
    ("Vavuniya", 8.7514, 80.4971), ("Mullaitivu", 9.2671, 80.8142), ("Batticaloa", 7.7310, 81.6747),
    ("Ampara", 7.2975, 81.6820), ("Trincomalee", 8.5874, 81.2152), ("Kurunegala", 7.4863, 80.3647),
    ("Puttalam", 8.0362, 79.8283), ("Anuradhapura", 8.3114, 80.4037), ("Polonnaruwa", 7.9403, 81.0188),
    ("Badulla", 6.9934, 81.0550), ("Monaragala", 6.8728, 81.3507), ("Ratnapura", 6.6828, 80.3992),
    ("Kegalle", 7.2513, 80.3464),
]

STATE_FILE = os.path.join(os.path.dirname(__file__), "daily_state.json")
DAILY_HOUR = 7  # morning briefing time (local)


def sri_lanka_weather():
    """Widget-ready dict for every district, or {'error': ...}."""
    try:
        r = requests.get(OPEN_METEO_URL, headers=UA, timeout=20, params={
            "latitude": ",".join(str(d[1]) for d in DISTRICTS),
            "longitude": ",".join(str(d[2]) for d in DISTRICTS),
            "timezone": "Asia/Colombo", "forecast_days": 1,
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        })
        data = r.json()
    except Exception as e:
        return {"error": f"Sri Lanka weather fetch failed: {e}"}
    if isinstance(data, dict):  # Open-Meteo returns an object (not a list) on error
        return {"error": data.get("reason", "Weather service returned no data.")}
    rows = []
    for (name, lat, lon), d in zip(DISTRICTS, data):
        cur, day = d.get("current", {}), d.get("daily", {})
        rain = (day.get("precipitation_probability_max") or [None])[0]
        rows.append({
            "district": name, "lat": lat, "lon": lon,
            "temp": round(cur.get("temperature_2m", 0)), "humidity": cur.get("relative_humidity_2m"),
            "wind": round(cur.get("wind_speed_10m", 0)), "description": WMO.get(cur.get("weather_code"), "unknown"),
            "hi": round((day.get("temperature_2m_max") or [0])[0]), "lo": round((day.get("temperature_2m_min") or [0])[0]),
            "rain_chance": rain,
        })
    hottest = max(rows, key=lambda x: x["temp"])
    coolest = min(rows, key=lambda x: x["temp"])
    wet = sorted([x for x in rows if (x["rain_chance"] or 0) >= 60], key=lambda x: -x["rain_chance"])
    summary = (f"Sri Lanka now: hottest {hottest['district']} {hottest['temp']}°C, coolest {coolest['district']} {coolest['temp']}°C. "
               + (f"High rain chance in {', '.join(x['district'] for x in wet[:6])}." if wet else "No district has a high rain chance today."))
    return {
        "updated": datetime.now().strftime("%a %d %b %Y, %H:%M"), "summary": summary, "districts": rows,
        "markers": [{"label": f"{x['district'].upper()[:12]} {x['temp']}°", "lat": x["lat"], "lon": x["lon"],
                     "hot": (x["rain_chance"] or 0) >= 60} for x in rows],
        "city": "Sri Lanka", "lat": 7.8731, "lon": 80.7718,
    }


def daily_due(now=None):
    """True once per day, from DAILY_HOUR onwards, until mark_daily_done() is called."""
    now = now or datetime.now()
    if now.hour < DAILY_HOUR:
        return False
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("weather_date") != now.date().isoformat()
    except (OSError, ValueError):
        return True


def mark_daily_done(day=None):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"weather_date": (day or date.today()).isoformat()}, f)
