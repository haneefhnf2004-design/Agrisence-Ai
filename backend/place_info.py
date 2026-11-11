"""Area details for show_map, like a Google place card. No API keys.
Address: Nominatim reverse. Story + photo: Wikipedia. Nearby places: OpenStreetMap Overpass.
Every source is optional: if one fails, the card just has less in it.
"""
import difflib
import math
import re
from concurrent.futures import ThreadPoolExecutor

import requests

UA = {"User-Agent": "Nexora-V2-Assistant/1.0 (contact: local app)", "Accept-Language": "en"}
WIKI_API = "https://en.wikipedia.org/w/api.php"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
]

# OSM tag -> friendly group name shown on the card
_GROUPS = {
    "hospital": "Hospitals", "clinic": "Hospitals", "school": "Schools", "university": "Schools",
    "college": "Schools", "place_of_worship": "Worship", "restaurant": "Food", "cafe": "Food",
    "fast_food": "Food", "bank": "Banks", "fuel": "Fuel", "bus_station": "Transport",
    "station": "Transport", "attraction": "Sights", "museum": "Sights", "viewpoint": "Sights",
    "hotel": "Hotels", "guest_house": "Hotels", "park": "Parks", "beach_resort": "Parks",
    "supermarket": "Shops", "mall": "Shops", "marketplace": "Shops", "monument": "Sights",
    "fort": "Sights", "ruins": "Sights", "temple": "Worship",
}


# Wikipedia articles near a point that are events or election areas, not places to visit
_NOT_A_PLACE = re.compile(r"\b(massacre|siege|battle|bombing|attack|riots?|electoral|election|polling|"
                          r"divisional secretariat|grama niladhari)\b|\b\d{4}\b", re.I)


def _km(lat1, lon1, lat2, lon2):
    r = math.radians
    a = (math.sin(r(lat2 - lat1) / 2) ** 2
         + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))


def _address(lat, lon):
    r = requests.get("https://nominatim.openstreetmap.org/reverse",
                     params={"lat": lat, "lon": lon, "format": "json", "zoom": 14, "accept-language": "en"},
                     headers=UA, timeout=10)
    a = (r.json() or {}).get("address", {})
    return {
        "area": a.get("suburb") or a.get("village") or a.get("town") or a.get("city") or a.get("county"),
        "district": a.get("state_district") or a.get("county"),
        "province": a.get("state"),
        "country": a.get("country"),
        "postcode": a.get("postcode"),
    }


def _wiki(name, lat, lon):
    """Wikipedia article about the place: by name first (if it is really near), else the closest one."""
    common = {"action": "query", "format": "json", "prop": "extracts|pageimages|coordinates|info",
              "exintro": 1, "explaintext": 1, "exsentences": 4, "piprop": "thumbnail",
              "pithumbsize": 480, "inprop": "url", "redirects": 1}
    pages = []
    r = requests.get(WIKI_API, params={**common, "titles": name}, headers=UA, timeout=10)
    pages += list((r.json().get("query") or {}).get("pages", {}).values())
    r = requests.get(WIKI_API, params={**common, "generator": "geosearch", "ggscoord": f"{lat}|{lon}",
                                       "ggsradius": 10000, "ggslimit": 10}, headers=UA, timeout=10)
    pages += list((r.json().get("query") or {}).get("pages", {}).values())

    best, best_score = None, -1.0
    for p in pages:
        if "missing" in p or not p.get("extract") or not p.get("coordinates"):
            continue
        c = p["coordinates"][0]
        dist = _km(lat, lon, c["lat"], c["lon"])
        if dist > 40:
            continue  # same name, different place
        sim = difflib.SequenceMatcher(None, name.lower(), p["title"].lower().split(",")[0]).ratio()
        score = sim * 2 - dist / 20
        if score > best_score:
            best, best_score = p, score
    # the other articles around the point double as nearby landmarks
    landmarks, seen = [], {best["title"] if best else None}
    for p in pages:
        if p.get("coordinates") and p.get("title") not in seen and not _NOT_A_PLACE.search(p["title"]):
            seen.add(p["title"])
            c = p["coordinates"][0]
            landmarks.append({"name": p["title"], "group": "Landmarks", "lat": c["lat"], "lon": c["lon"],
                              "dist_m": int(_km(lat, lon, c["lat"], c["lon"]) * 1000)})
    landmarks.sort(key=lambda p: p["dist_m"])
    if not best:
        return {"landmarks": landmarks[:8]} if landmarks else None
    return {"title": best["title"], "summary": best["extract"].strip(), "landmarks": landmarks[:8],
            "image": (best.get("thumbnail") or {}).get("source"), "url": best.get("fullurl")}


def _nearby(lat, lon, radius_m=1500):
    q = f"""[out:json][timeout:12];
(
  nwr(around:{radius_m},{lat},{lon})[name][amenity~"hospital|clinic|school|university|college|place_of_worship|restaurant|cafe|fast_food|bank|fuel|bus_station|marketplace"];
  nwr(around:{radius_m},{lat},{lon})[name][tourism~"attraction|museum|viewpoint|hotel|guest_house"];
  nwr(around:{radius_m},{lat},{lon})[name][historic~"monument|fort|ruins"];
  nwr(around:{radius_m},{lat},{lon})[name][leisure~"park|beach_resort"];
  nwr(around:{radius_m},{lat},{lon})[name][shop~"supermarket|mall"];
  nwr(around:{radius_m},{lat},{lon})[name][railway=station];
);
out center 200;"""
    elements = None
    for url in OVERPASS:  # public servers are often busy; try the next one
        try:
            r = requests.post(url, data={"data": q}, headers=UA, timeout=7)
            elements = r.json().get("elements", [])
            break
        except Exception as e:
            print(f"[PLACE] overpass {url} failed: {e}")
    if elements is None:
        return None
    places, counts, seen = [], {}, set()
    for e in elements:
        t = e.get("tags", {})
        kind = (t.get("amenity") or t.get("tourism") or t.get("historic") or t.get("leisure")
                or t.get("shop") or ("station" if t.get("railway") else None))
        group = _GROUPS.get(kind)
        name = t.get("name:en") or t.get("name")
        if not group or not name or name in seen:
            continue
        seen.add(name)
        plat = e.get("lat") or (e.get("center") or {}).get("lat")
        plon = e.get("lon") or (e.get("center") or {}).get("lon")
        if plat is None:
            continue
        counts[group] = counts.get(group, 0) + 1
        places.append({"name": name, "group": group, "lat": plat, "lon": plon,
                       "dist_m": int(_km(lat, lon, plat, plon) * 1000)})
    # sights first, then the closest
    places.sort(key=lambda p: (p["group"] != "Sights", p["dist_m"]))
    return {"counts": counts, "places": places[:12]}


def place_details(name, lat, lon):
    """All sources at once; whatever answers in time goes on the card."""
    jobs = {"address": (_address, (lat, lon)), "wiki": (_wiki, (name, lat, lon)), "nearby": (_nearby, (lat, lon))}
    out = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {k: pool.submit(f, *a) for k, (f, a) in jobs.items()}
        for k, fut in futs.items():
            try:
                out[k] = fut.result(timeout=26)
            except Exception as e:
                print(f"[PLACE] {k} failed: {e}")
                out[k] = None
    w = out.get("wiki") or {}
    if not (out.get("nearby") or {}).get("places") and w.get("landmarks"):
        out["nearby"] = {"counts": {}, "places": w["landmarks"]}
    if w and not w.get("summary"):
        out["wiki"] = None
    return out


def spoken_summary(name, info):
    """Short text handed back to the model so Nexora can talk about the place."""
    bits = [f"Map locked on {name} in satellite 3D view."]
    a = info.get("address") or {}
    where = ", ".join(x for x in (a.get("district"), a.get("province"), a.get("country")) if x)
    if where:
        bits.append(f"Location: {where}.")
    w = info.get("wiki")
    if w:
        bits.append(f"About ({w['title']}): {w['summary'][:600]}")
    n = info.get("nearby") or {}
    if n.get("counts"):
        bits.append("Within 1.5 km: " + ", ".join(f"{v} {k.lower()}" for k, v in n["counts"].items()) + ".")
    if n.get("places"):
        bits.append("Nearby: " + "; ".join(f"{p['name']} ({p['group'].lower()}, {p['dist_m']} m)" for p in n["places"][:6]) + ".")
    return " ".join(bits)
