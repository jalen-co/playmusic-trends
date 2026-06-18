#!/usr/bin/env python3
"""
PlayMusic Trend Monitor — pull (v4.1)
Hardened: reduced lookup budget, all sections wrapped so partial failures
don't kill the run, 6-minute timeout on genre lookups.
"""

import datetime as dt
import io
import json
import os
import re
import sys
import time
import html as html_mod

import requests
import pandas as pd

HEADERS = {"User-Agent": "PlayMusic-TrendMonitor/4.1 (internal ops)"}
CACHE_FILE = "genre_cache.json"

KWORB = "https://kworb.net/spotify/country/{cc}_{period}.html"
APPLE_TOP = "https://rss.applemarketingtools.com/api/v2/{cc}/music/most-played/200/songs.json"
APPLE_GENRE = "https://itunes.apple.com/{cc}/rss/topsongs/limit=100/genre={gid}/json"
ITUNES_SEARCH = "https://itunes.apple.com/search"

PERIODS = ["daily", "weekly"]
MARKETS = ["us", "gb", "ca", "au", "de", "fr", "es", "it", "nl", "se",
           "br", "mx", "jp", "kr", "in"]
LABELS = {"global": "Global", "europe": "Europe", "us": "US", "gb": "UK",
          "ca": "Canada", "au": "Australia", "de": "Germany", "fr": "France",
          "es": "Spain", "it": "Italy", "nl": "Netherlands", "se": "Sweden",
          "br": "Brazil", "mx": "Mexico", "jp": "Japan", "kr": "South Korea",
          "in": "India"}
EU_AGG = ["gb", "de", "fr", "es", "it", "nl", "se"]
GENRE_STOREFRONTS = ["us", "gb", "de", "fr", "es", "it", "au", "ca"]
REGION_ORDER = ["global", "us", "europe", "gb", "ca", "au", "de", "fr", "es", "it",
                "nl", "se", "br", "mx", "jp", "kr", "in"]

GENRES = {"Pop": 14, "Hip-Hop/Rap": 18, "Country": 6, "R&B/Soul": 15,
          "Dance": 17, "Electronic": 7, "Rock": 21, "Alternative": 20,
          "Latin": 12, "K-Pop": 51}
GENRE_MARKETS = ["us", "gb"]
RECENCY_DAYS = 730
MAX_SEARCH = 500          # reduced from 1200 — builds up over runs via cache
SEARCH_TIME_LIMIT = 300   # stop lookups after 5 minutes regardless


def norm_key(artist, title):
    s = f"{artist} {title}".lower()
    s = html_mod.unescape(s)
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = re.sub(r"\(feat\.?.*?\)|\bfeat\.?.*$|\(.*?\)|\[.*?\]|\(w/.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_text(s):
    if not s:
        return s
    s = html_mod.unescape(s)
    s = re.sub(r'â[\x80-\xbf][\x80-\xbf]?', "'", s)
    return s.strip()


def _to_int(x):
    try:
        return int(str(x).replace(",", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return None


def safe_get(url, **kw):
    """requests.get with retry on 429/5xx."""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, **kw)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 5))
                print(f"  429 rate-limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                time.sleep(2)
                continue
            raise
    return None


def fetch_kworb(cc, period):
    r = safe_get(KWORB.format(cc=cc, period=period))
    tables = pd.read_html(io.StringIO(r.text))
    table = next((t for t in tables if any("Artist" in str(c) for c in t.columns)),
                 tables[0] if tables else None)
    if table is None:
        raise RuntimeError(f"No chart table {cc}/{period}")
    table.columns = [str(c).strip() for c in table.columns]
    rows = []
    for _, row in table.iterrows():
        at = str(row.get("Artist and Title", "")).strip()
        if " - " not in at:
            continue
        artist, title = at.split(" - ", 1)
        rk = _to_int(row.get("Pos"))
        if not rk:
            continue
        artist, title = clean_text(artist), clean_text(title)
        rows.append({"rank": rk, "artist": artist, "title": title,
                     "move": str(row.get("P+", "")).strip(),
                     "streams": _to_int(row.get("Streams")),
                     "key": norm_key(artist, title)})
    return rows


def europe_aggregate(raw, period):
    agg = {}
    for cc in EU_AGG:
        for r in raw.get((cc, period), []):
            a = agg.setdefault(r["key"], {"artist": r["artist"], "title": r["title"],
                                          "points": 0, "streams": 0})
            a["points"] += max(0, 201 - r["rank"])
            a["streams"] += r["streams"] or 0
    ordered = sorted(agg.values(), key=lambda x: x["points"], reverse=True)
    return [{"rank": i, "artist": x["artist"], "title": x["title"], "move": "",
             "streams": x["streams"], "key": norm_key(x["artist"], x["title"])}
            for i, x in enumerate(ordered[:200], start=1)]


def fetch_apple_top(cc):
    r = safe_get(APPLE_TOP.format(cc=cc))
    out = {}
    for t in r.json()["feed"]["results"]:
        g = [x["name"] for x in t.get("genres", []) if x["name"] != "Music"]
        out[norm_key(t["artistName"], t["name"])] = g[0] if g else ""
    return out


def fetch_genre_chart(cc, gid, genre_name):
    url = APPLE_GENRE.format(cc=cc, gid=gid)
    r = safe_get(url)
    data = r.json()
    entries = data.get("feed", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RECENCY_DAYS)
    rows = []
    for e in entries:
        try:
            title = e["im:name"]["label"]
            artist = e["im:artist"]["label"]
        except (KeyError, TypeError):
            continue
        rd = e.get("im:releaseDate", {}).get("label", "")
        if rd:
            try:
                rel = dt.datetime.fromisoformat(rd.replace("Z", "+00:00"))
                if rel < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
        rows.append({"title": clean_text(title), "artist": clean_text(artist),
                     "genre": genre_name, "key": norm_key(artist, title)})
    return rows


def search_genre(artist, title):
    try:
        r = requests.get(ITUNES_SEARCH, headers=HEADERS, timeout=10, params={
            "term": f"{artist} {title}", "media": "music", "entity": "song", "limit": 1})
        if r.status_code == 429:
            return ""
        r.raise_for_status()
        res = r.json().get("results", [])
        return res[0].get("primaryGenreName", "") if res else ""
    except Exception:
        return ""


def main():
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            cache = json.load(open(CACHE_FILE))
        except Exception:
            cache = {}

    # 1. Charts
    print("Pulling charts...")
    raw = {}
    for cc in MARKETS + ["global"]:
        for p in PERIODS:
            try:
                raw[(cc, p)] = fetch_kworb(cc, p)
            except Exception as e:
                print(f"  {cc}/{p} skipped: {type(e).__name__}: {e}")
                raw[(cc, p)] = []
            time.sleep(0.15)

    data = {}
    for cc in MARKETS + ["global"]:
        data[cc] = {p: raw.get((cc, p), []) for p in PERIODS}
    data["europe"] = {p: europe_aggregate(raw, p) for p in PERIODS}

    # 2. Genre map (Apple most-played)
    print("Building genre map...")
    gmap = {}
    for cc in GENRE_STOREFRONTS:
        try:
            gm = fetch_apple_top(cc)
            for k, g in gm.items():
                if g and k not in gmap:
                    gmap[k] = g
        except Exception as e:
            print(f"  genre-tags {cc} skipped: {type(e).__name__}: {e}")
        time.sleep(0.2)
    print(f"  Apple tags: {len(gmap)}")

    # 3. Search remaining (time-boxed)
    print("Genre lookups (time-boxed)...")
    best = {}
    for reg in data.values():
        for rows in reg.values():
            for r in rows:
                k = r["key"]
                if k not in best or r["rank"] < best[k][0]:
                    best[k] = (r["rank"], r["artist"], r["title"])
    pending = [(rk, k, a, t) for k, (rk, a, t) in best.items()
               if not (gmap.get(k) or cache.get(k))]
    pending.sort()
    lookups, t0 = 0, time.time()
    for rk, k, a, t in pending:
        if lookups >= MAX_SEARCH or (time.time() - t0) > SEARCH_TIME_LIMIT:
            break
        g = search_genre(a, t)
        if g:
            cache[k] = g
        lookups += 1
        time.sleep(0.5)
    elapsed = int(time.time() - t0)
    print(f"  lookups: {lookups} in {elapsed}s | cached: {len(cache)}")

    def genre_for(key):
        return gmap.get(key) or cache.get(key) or ""

    all_genres = set()
    for reg in data.values():
        for rows in reg.values():
            for r in rows:
                r["genre"] = genre_for(r["key"])
                if r["genre"]:
                    all_genres.add(r["genre"])
                r.pop("key", None)

    # 4. Per-genre charts
    print("Building per-genre charts...")
    genre_charts = {}
    for gname, gid in GENRES.items():
        seen = set()
        combined = []
        for cc in GENRE_MARKETS:
            try:
                rows = fetch_genre_chart(cc, gid, gname)
                for r in rows:
                    if r["key"] not in seen:
                        seen.add(r["key"])
                        combined.append(r)
            except Exception as e:
                print(f"  genre {gname}/{cc} skipped: {type(e).__name__}: {e}")
            time.sleep(0.3)
        for i, r in enumerate(combined, start=1):
            r["rank"] = i
            r.pop("key", None)
        genre_charts[gname] = combined
        if combined:
            all_genres.add(gname)

    print(f"  genre charts: { {g: len(v) for g, v in genre_charts.items()} }")

    # 5. Save
    json.dump(cache, open(CACHE_FILE, "w"))
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "regions": [r for r in REGION_ORDER if r in data],
        "region_labels": LABELS,
        "periods": PERIODS,
        "genres": sorted(all_genres),
        "genre_charts": genre_charts,
        "data": data,
    }
    json.dump(payload, open("trend_latest.json", "w"), separators=(",", ":"), default=str)

    tagged = sum(1 for reg in data.values() for rows in reg.values()
                 for r in rows if r.get("genre"))
    total = sum(len(rows) for reg in data.values() for rows in reg.values())
    print(f"\nDone. regions: {len(data)} | songs: {total} | tagged: {tagged}/{total}")


if __name__ == "__main__":
    main()
