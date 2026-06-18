#!/usr/bin/env python3
"""
PlayMusic Trend Monitor — pull (v3.1)

One accurate chart per region+period, in true rank order (1..N), from current
Spotify streaming (via Kworb). Daily and Weekly. Many markets + Europe aggregate
+ Global. Genre attached per song (top-of-chart prioritized) for filtering.

Run:  python trend_pull.py
Deps: pip install requests pandas lxml
"""

import datetime as dt
import io
import json
import os
import re
import sys
import time

import requests
import pandas as pd

HEADERS = {"User-Agent": "PlayMusic-TrendMonitor/3.1 (internal ops)"}
CACHE_FILE = "genre_cache.json"

KWORB = "https://kworb.net/spotify/country/{cc}_{period}.html"
APPLE_TOP = "https://rss.applemarketingtools.com/api/v2/{cc}/music/most-played/200/songs.json"
ITUNES_SEARCH = "https://itunes.apple.com/search"

PERIODS = ["daily", "weekly"]
# Standalone markets (each a real Spotify chart). Order = dropdown order.
MARKETS = ["us", "gb", "ca", "au", "de", "fr", "es", "it", "nl", "se",
           "br", "mx", "jp", "kr", "in"]
LABELS = {"global": "Global", "europe": "Europe", "us": "US", "gb": "UK",
          "ca": "Canada", "au": "Australia", "de": "Germany", "fr": "France",
          "es": "Spain", "it": "Italy", "nl": "Netherlands", "se": "Sweden",
          "br": "Brazil", "mx": "Mexico", "jp": "Japan", "kr": "South Korea",
          "in": "India"}
EU_AGG = ["gb", "de", "fr", "es", "it", "nl", "se"]
GENRE_STOREFRONTS = ["us", "gb", "de", "fr", "es", "it", "br", "mx"]
REGION_ORDER = ["global", "us", "gb", "ca", "au", "de", "fr", "es", "it",
                "nl", "se", "br", "mx", "jp", "kr", "in", "europe"]
MAX_SEARCH_LOOKUPS = 800


def norm_key(artist, title):
    s = f"{artist} {title}".lower()
    s = re.sub(r"\(feat\.?.*?\)|\bfeat\.?.*$|\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _to_int(x):
    try:
        return int(str(x).replace(",", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return None


def fetch_kworb(cc, period):
    r = requests.get(KWORB.format(cc=cc, period=period), headers=HEADERS, timeout=20)
    r.raise_for_status()
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
        rows.append({"rank": rk, "artist": artist.strip(), "title": title.strip(),
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


def build_genre_map():
    gmap = {}
    for cc in GENRE_STOREFRONTS:
        try:
            r = requests.get(APPLE_TOP.format(cc=cc), headers=HEADERS, timeout=20)
            r.raise_for_status()
            for t in r.json()["feed"]["results"]:
                g = [x["name"] for x in t.get("genres", []) if x["name"] != "Music"]
                if g:
                    gmap.setdefault(norm_key(t["artistName"], t["name"]), g[0])
        except Exception as e:
            print(f"  genre-tags {cc} skipped: {type(e).__name__}: {e}")
        time.sleep(0.2)
    return gmap


def search_genre(artist, title):
    try:
        r = requests.get(ITUNES_SEARCH, headers=HEADERS, timeout=15, params={
            "term": f"{artist} {title}", "media": "music", "entity": "song", "limit": 1})
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

    print("Pulling charts (markets x periods)...")
    raw = {}
    for cc in MARKETS + ["global"]:
        for p in PERIODS:
            try:
                raw[(cc, p)] = fetch_kworb(cc, p)
            except Exception as e:
                print(f"  {cc}/{p} skipped: {type(e).__name__}: {e}")
                raw[(cc, p)] = []
            time.sleep(0.15)

    if not any(raw.values()):
        sys.exit("All chart pulls failed.")

    # Assemble region -> period -> rows
    data = {}
    for cc in MARKETS + ["global"]:
        data[cc] = {p: raw.get((cc, p), []) for p in PERIODS}
    data["europe"] = {p: europe_aggregate(raw, p) for p in PERIODS}

    print("Tagging genres...")
    gmap = build_genre_map()

    # unique tracks with best (lowest) rank -> prioritize top of charts for lookups
    best = {}
    for reg in data.values():
        for rows in reg.values():
            for r in rows:
                k = r["key"]
                if k not in best or r["rank"] < best[k][0]:
                    best[k] = (r["rank"], r["artist"], r["title"])
    pending = [(rk, k, a, t) for k, (rk, a, t) in best.items()
               if not (gmap.get(k) or cache.get(k))]
    pending.sort()  # lowest rank first
    lookups = 0
    for rk, k, a, t in pending:
        if lookups >= MAX_SEARCH_LOOKUPS:
            break
        cache[k] = search_genre(a, t)
        lookups += 1
        time.sleep(0.6)
    print(f"  genre lookups this run: {lookups} (cached: {len(cache)})")

    genres_seen = set()
    for reg in data.values():
        for rows in reg.values():
            for r in rows:
                r["genre"] = gmap.get(r["key"]) or cache.get(r["key"]) or ""
                if r["genre"]:
                    genres_seen.add(r["genre"])
                r.pop("key", None)

    json.dump(cache, open(CACHE_FILE, "w"))
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "regions": [r for r in REGION_ORDER if r in data],
        "region_labels": LABELS,
        "periods": PERIODS,
        "genres": sorted(genres_seen),
        "data": data,
    }
    json.dump(payload, open("trend_latest.json", "w"), separators=(",", ":"), default=str)
    counts = {r: len(data[r]["daily"]) for r in data}
    print("Done. regions:", len(data), "| daily counts:", counts, "| genres:", len(genres_seen))


if __name__ == "__main__":
    main()
