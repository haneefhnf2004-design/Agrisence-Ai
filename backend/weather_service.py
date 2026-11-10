"""Keyless city geocoding + weather. No API keys needed.
Geocode: Nominatim (OSM) with Photon fallback. Weather: Open-Meteo.
"""
import difflib
import json
import os
import re

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
PHOTON_URL = "https://photon.komoot.io/api/"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
UA = {"User-Agent": "Nexora-V2-Assistant/1.0 (contact: local app)", "Accept-Language": "en"}

WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "icy fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
    81: "showers", 82: "violent showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "storm with hail", 99: "storm with hail",
}


# ---- Sri Lanka first place search -------------------------------------------------------------
# The user lives in Sri Lanka, so a bare name like "Hatton" or "Ella" should mean the Sri Lankan one.
# 1) cache (instant, offline)  2) OpenStreetMap search inside Sri Lanka, accepted only if the name
# really matches  3) worldwide search (best-ranked hit)  4) Photon, biased toward Sri Lanka.
_CACHE_FILE = os.path.join(os.path.dirname(__file__), "places_cache.json")
_LK_CENTER = (7.87, 80.77)
_PLACE_RANK = {"city": 0, "town": 1, "municipality": 1, "suburb": 2, "village": 2, "neighbourhood": 3,
               "hamlet": 3, "quarter": 3, "administrative": 1, "county": 1, "state": 1}
_FOREIGN_HINT = re.compile(r",|\b(india|usa|uk|england|london|australia|canada|china|japan|dubai|uae|"
                           r"singapore|maldives|france|germany|italy|qatar|saudi|malaysia|thailand)\b", re.I)


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _load_cache():
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_cache = _load_cache()


def _remember(key, lat, lon, name):
    _cache[key] = [lat, lon, name]
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
    except Exception:
        pass


def _seed_districts():
    try:
        from lk_weather import DISTRICTS
        for name, lat, lon in DISTRICTS:
            _cache.setdefault(_norm(name), [lat, lon, name])
    except Exception:
        pass


def _nominatim(q, lk_only):
    params = {"q": q, "format": "json", "limit": 8, "accept-language": "en"}
    if lk_only:
        params["countrycodes"] = "lk"
    r = requests.get(NOMINATIM_URL, params=params, headers=UA, timeout=12)
    return r.json() or []


def _best(hits, query, strict):
    """Pick the hit whose name best matches the query; prefer real places and higher OSM importance."""
    if not strict:
        hits = hits[:1]  # worldwide: Nominatim already ranks by importance (London -> UK, not Ontario)
    qn = _norm(query)
    scored = []
    for h in hits:
        name = (h.get("name") or h.get("display_name", "").split(",")[0]).strip()
        sim = difflib.SequenceMatcher(None, qn, _norm(name)).ratio()
        is_place = h.get("class") in ("place", "boundary")
        # Inside Sri Lanka only accept real places with a close name,
        # so "Paris" never becomes "Paris Perera Lane, Colombo".
        if strict and (sim < 0.8 or not is_place):
            continue
        kind = h.get("type") if is_place else None
        rank = _PLACE_RANK.get(kind, 5)
        scored.append((sim - rank * 0.05 + float(h.get("importance") or 0) * 0.3, name, h))
    if not scored:
        return None
    _, name, h = max(scored, key=lambda x: x[0])
    return float(h["lat"]), float(h["lon"]), name


def geocode_city(city):
    """Returns (lat, lon, display_name) or {'error': ...}. Sri Lanka first, then the world."""
    city = (city or "").strip()
    if not city:
        return {"error": "No city given."}
    key = _norm(city)
    if "colombo" not in _cache:
        _seed_districts()  # imported lazily: lk_weather imports this module
    if key in _cache:
        lat, lon, name = _cache[key]
        return (lat, lon, name)
    foreign = bool(_FOREIGN_HINT.search(city))
    try:
        lk_hits = [] if foreign else _nominatim(city, True)
        hit = _best(lk_hits, city, strict=True)
        if not hit:
            world = _nominatim(city, False)
            # Areas like Pettah exist in OSM only as landmarks ("Pettah Market"). Use those unless the
            # worldwide match is a well-known place (Paris, London) — then the user means that one.
            named = [h for h in lk_hits if re.search(rf"\b{re.escape(_norm(city))}\b", _norm(h.get("name")))]
            if named and (not world or float(world[0].get("importance") or 0) < 0.5):
                hit = (float(named[0]["lat"]), float(named[0]["lon"]), city.title())
            else:
                hit = _best(world, city, strict=False)
        if hit:
            _remember(key, *hit)
            return hit
    except Exception as e:
        print(f"[GEOCODE] Nominatim failed: {e}")
    try:
        r = requests.get(PHOTON_URL, params={"q": city, "limit": 1, "lat": _LK_CENTER[0], "lon": _LK_CENTER[1]}, timeout=12)
        feats = r.json().get("features", [])
        if feats:
            lon, lat = feats[0]["geometry"]["coordinates"]
            props = feats[0].get("properties", {})
            name = props.get("name", city)
            _remember(key, float(lat), float(lon), name)
            return (float(lat), float(lon), name)
    except Exception as e:
        print(f"[GEOCODE] Photon failed: {e}")
    return {"error": f"Could not find city '{city}'."}


def fetch_weather(lat, lon):
    """Returns Open-Meteo current + 3-day forecast dict or {'error': ...}."""
    try:
        r = requests.get(OPEN_METEO_URL, params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 4,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        }, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": f"Weather fetch failed: {e}"}


def city_weather(city):
    """Full pipeline: geocode -> weather -> widget-ready dict."""
    geo = geocode_city(city)
    if isinstance(geo, dict) and "error" in geo:
        return geo
    lat, lon, name = geo
    wx = fetch_weather(lat, lon)
    if "error" in wx:
        return wx
    cur = wx.get("current", {})
    daily = wx.get("daily", {})
    code = cur.get("weather_code", 0)
    desc = WMO.get(code, "unknown")
    dates = daily.get("time", [])[1:4]
    codes = daily.get("weather_code", [])[1:4]
    his = daily.get("temperature_2m_max", [])[1:4]
    los = daily.get("temperature_2m_min", [])[1:4]
    forecast = [{"date": d[5:], "desc": WMO.get(c, "--"),
                 "lo": round(lo), "hi": round(hi)}
                for d, c, lo, hi in zip(dates, codes, los, his)]
    return {
        "city": name, "temp": round(cur.get("temperature_2m", 0)),
        "feels": round(cur.get("apparent_temperature", 0)),
        "description": desc, "humidity": cur.get("relative_humidity_2m", 0),
        "wind": f"{round(cur.get('wind_speed_10m', 0))} km/h",
        "forecast": forecast, "lat": lat, "lon": lon,
        "markers": [{"label": name.upper()[:16], "lat": lat, "lon": lon, "hot": True}],
    }
