"""Live Sri Lanka news with photos (keyless).

Sources, merged newest first:
- Google News search: widest and freshest coverage (no photos, wrapped links)
- Sri Lankan outlets' own feeds: Daily Mirror breaking news, EconomyNext, Newswire
- Bing News: photos and Sinhala/Tamil outlets
Each top story gets its photo from the article page's og:image tag.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
import re
import xml.etree.ElementTree as ET

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Nexora/2"}
LK_TZ = timezone(timedelta(hours=5, minutes=30))

# Google's "Sri Lanka edition" redirects to US/India news, so search for Sri Lanka in each language instead.
GOOGLE = {
    "en": ("Sri Lanka", "hl=en-US&gl=US&ceid=US:en"),
    "ta": ("இலங்கை", "hl=ta&gl=IN&ceid=IN:ta"),
    "si": ("ශ්‍රී ලංකා", "hl=si&gl=LK&ceid=LK:si"),
}
LOCAL_FEEDS = [
    "https://www.dailymirror.lk/rss/breaking_news/108",
    "https://economynext.com/feed/",
    "https://www.newswire.lk/feed/",
]
OG_IMAGE = [re.compile(p, re.I) for p in (
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
)]


def _ago(ts):
    mins = int((datetime.now(timezone.utc).timestamp() - ts) // 60)
    if mins < 60:
        return f"{max(mins, 1)} min ago"
    if mins < 1440:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


def _date(text):
    text = (text or "").strip()
    try:
        return parsedate_to_datetime(text)
    except Exception:
        pass
    try:  # Daily Mirror uses "2026-11-10 11:37:00" in Sri Lanka time
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LK_TZ)
    except Exception:
        return None


def _rss(url):
    """(item element, {local tag name: text}) pairs from an RSS feed; [] on any failure."""
    try:
        r = requests.get(url, headers=UA, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"[NEWS] {url[:60]} failed: {e}")
        return []
    # Bing puts its extra tags in a namespace named after the query URL, so key by local name.
    return [{c.tag.split("}")[-1]: (c.text or "").strip() for c in it} for it in root.iter("item")]


def _item(title, source, link, dt, image=""):
    return {"title": title, "source": source, "link": link, "image": image,
            "time": dt.astimezone(LK_TZ).strftime("%I:%M %p, %d %b"), "_ts": dt.timestamp()}


def _google(q, params, window):
    out = []
    for x in _rss(f"https://news.google.com/rss/search?q={quote_plus(q + ' ' + window)}&{params}"):
        dt, title, source = _date(x.get("pubDate")), x.get("title", ""), x.get("source", "")
        if dt and title:
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3]
            out.append(_item(title, source, x.get("link", ""), dt))
    return out


def _bing(q):
    out = []
    for x in _rss(f"https://www.bing.com/news/search?q={quote_plus(q)}&format=rss"):
        dt = _date(x.get("pubDate"))
        if dt and x.get("title"):
            img = x.get("Image", "").replace("http://", "https://")
            out.append(_item(x["title"], x.get("Source", ""), x.get("link", ""), dt, img))
    return out


def _local(topic):
    out = []
    for url in LOCAL_FEEDS:
        source = url.split("/")[2].replace("www.", "")
        for x in _rss(url):
            dt, title = _date(x.get("pubDate")), x.get("title", "")
            if dt and title and (not topic or topic.lower() in (title + " " + x.get("description", "")).lower()):
                out.append(_item(title, source, x.get("link", ""), dt))
    return out


def _og_image(url):
    try:
        html = requests.get(url, headers=UA, timeout=6).text[:300000]
    except Exception:
        return ""
    for p in OG_IMAGE:
        m = p.search(html)
        if m:
            return m.group(1).replace("&amp;", "&")
    return ""


def fetch(topic=None, hours=24, lang="en", limit=10):
    """Newest Sri Lanka stories (optionally about a topic), each with a photo where one exists."""
    word, params = GOOGLE.get(lang, GOOGLE["en"])
    q = f"{word} {topic}".strip() if topic else word
    window = f"when:{int(hours)}h" if hours < 48 else f"when:{max(1, int(hours) // 24)}d"
    with ThreadPoolExecutor(5) as pool:
        jobs = [pool.submit(_google, q, params, window), pool.submit(_bing, q)]
        if lang == "en":
            jobs.append(pool.submit(_local, topic))
        items = [x for j in jobs for x in j.result()]
    if not items:
        return {"error": "Could not reach any news source. Check the internet connection."}

    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    items = sorted((x for x in items if x["_ts"] >= cutoff), key=lambda x: x["_ts"], reverse=True)
    by_key = {}
    for x in items:  # the same story often comes from several outlets
        key = re.sub(r"\W+", "", x["title"].lower())[:50]
        first = by_key.setdefault(key, x)
        if first is not x and "news.google.com" in first["link"] and "news.google.com" not in x["link"]:
            first.update(link=x["link"], image=first["image"] or x["image"])  # keep a direct link (has a photo)
    top = list(by_key.values())[:limit]

    # Photos: Google links are wrapped and carry none, every other link points at the article itself.
    need = [x for x in top if not x["image"] and "news.google.com" not in x["link"]]
    with ThreadPoolExecutor(8) as pool:
        for x, img in zip(need, pool.map(lambda x: _og_image(x["link"]), need)):
            x["image"] = img
    for x in top:
        x["ago"] = _ago(x.pop("_ts"))
    return {"query": q, "items": top}


def spoken(res):
    if "error" in res:
        return res["error"]
    if not res["items"]:
        return f"No Sri Lanka news found for '{res['query']}' in that time window."
    return "Newest first: " + " | ".join(f"{x['title']} ({x['source']}, {x['ago']})" for x in res["items"])


class BreakingWatcher:
    """Remembers headlines already seen so only new ones are announced."""

    def __init__(self):
        self.seen = None  # filled silently on the first check

    def new_items(self):
        res = fetch(hours=3, limit=15)
        if "error" in res:
            return []
        keys = {re.sub(r"\W+", "", x["title"].lower())[:50] for x in res["items"]}
        if self.seen is None:
            self.seen = keys
            return []
        fresh = [x for x in res["items"] if re.sub(r"\W+", "", x["title"].lower())[:50] not in self.seen]
        self.seen |= keys
        return fresh
