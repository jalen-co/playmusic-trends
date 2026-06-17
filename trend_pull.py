#!/usr/bin/env python3
"""
PlayMusic Trend Monitor — pull (v3)

One accurate chart per region, in true rank order (1..N), from current
Spotify streaming data (via Kworb). Genre is attached per song so it can be
used as a filter. No re-sorting, no sales-chart catalog noise.

Regions: US, Europe (points-aggregate of major EU markets), Global.
Genre source: Apple most-played genre tags + iTunes Search API (cached).

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

HEADERS = {"User-Agent": "PlayMusic-TrendMonitor/3.0 (internal ops)"}
CACHE_FILE = "genre_cache.json"

KWORB = "https://kworb.net/spotify/country/{cc}_daily.html"
APPLE_TOP = "https://rss.applemarketingtools.com/api/v2/{cc}/music/most-played/200/songs.json"
ITUNES_SEARCH = "https://itunes.apple.com/search"

EU_MARKETS = ["gb", "de", "fr", "es", "it", "nl", "se"]
GENRE_STOREFRONTS = ["us", "gb", "de", "fr", "es", "it", "nl", "se"]  # for free genre tags
MAX_SEARCH_LOOKUPS = 600  # safety cap per run


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


def fetch_kworb(cc):
    """Spotify daily Top 200 for a market, already in rank order."""
    r = requests.get(KWORB.format(cc=cc), headers=HEADERS, timeout=20)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    table = next((t for t in tables if any("Artist" in str(c) for c in t.columns)),
                 tables[0] if tables else None)
    if table is None:
        raise RuntimeError(f"No chart table for {cc}")
    table.columns = [str(c).strip() for c in table.columns]
    rows = []
    for _, row in table.iterrows():
        at = str(row.get("Artist and Title", "")).strip()
        if " - " not in at:
            continue
        artist, title = at.split(" - ", 1)
        rows.append({
            "rank": _to_int(row.get("Pos")),
            "artist": artist.strip(), "title": title.strip(),
            "move": str(row.get("P+", "")).strip(),
            "streams": _to_int(row.get("Streams")),
            "d7": _to_int(row.get("7Day+")),
            "key": norm_key(artist, title),
        })
    return [r for r in rows if r["rank"]]


def build_europe():
    """Points-aggregate of EU markets -> single ranked Europe chart."""
    agg = {}
    for cc in EU_MARKETS:
        try:
            for r in fetch_kworb(cc):
                pts = max(0, 201 - r["rank"])
                a = agg.setdefault(r["key"], {"artist": r["artist"], "title": r["title"],
                                              "points": 0, "streams": 0})
                a["points"] += pts
                a["streams"] += r["streams"] or 0
        except Exception as e:
            print(f"  europe market {cc} skipped: {type(e).__name__}: {e}")
        time.sleep(0.2)
    ordered = sorted(agg.values(), key=lambda x: x["points"], reverse=True)
    out = []
    for i, x in enumerate(ordered[:200], start=1):
        out.append({"rank": i, "artist": x["artist"], "title": x["title"],
                    "move": "", "streams": x["streams"], "d7": None,
                    "key": norm_key(x["artist"], x["title"])})
    return out


def build_genre_map():
    """Free, reliable genre tags from Apple most-played across storefronts."""
    gmap = {}
    for cc in GENRE_STOREFRONTS:
        try:
            r = requests.get(APPLE_TOP.format(cc=cc), headers=HEADERS, timeout=20)
            r.raise_for_status()
            for t in r.json()["feed"]["results"]:
                g = [x["name"] for x in t.get("genres", []) if x["name"] != "Music"]
                if g:
                    gmap[norm_key(t["artistName"], t["name"])] = g[0]
        except Exception as e:
            print(f"  genre-tags {cc} skipped: {type(e).__name__}: {e}")
        time.sleep(0.2)
    return gmap


def search_genre(artist, title):
    """Best-effort genre via iTunes Search API."""
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

    print("Pulling charts...")
    regions = {}
    try:
        regions["us"] = fetch_kworb("us")
        regions["global"] = fetch_kworb("global")
        regions["europe"] = build_europe()
    except Exception as e:
        sys.exit(f"Chart pull failed: {type(e).__name__}: {e}")

    print("Tagging genres (free Apple tags)...")
    gmap = build_genre_map()

    # collect unique tracks needing a genre, fill from map/cache, then search.
    unique = {}
    for rows in regions.values():
        for r in rows:
            unique.setdefault(r["key"], (r["artist"], r["title"]))
    lookups = 0
    for key, (artist, title) in unique.items():
        if gmap.get(key) or cache.get(key):
            continue
        if lookups >= MAX_SEARCH_LOOKUPS:
            break
        cache[key] = search_genre(artist, title)
        lookups += 1
        time.sleep(0.7)
    print(f"  genre lookups this run: {lookups}")

    def genre_for(key):
        return gmap.get(key) or cache.get(key) or ""

    genres_seen = set()
    for rows in regions.values():
        for r in rows:
            r["genre"] = genre_for(r["key"])
            if r["genre"]:
                genres_seen.add(r["genre"])
            r.pop("key", None)

    json.dump(cache, open(CACHE_FILE, "w"))
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "regions": ["us", "europe", "global"],
        "region_labels": {"us": "US", "europe": "Europe", "global": "Global"},
        "genres": sorted(genres_seen),
        "data": regions,
    }
    json.dump(payload, open("trend_latest.json", "w"), separators=(",", ":"), default=str)
    print("Done:", {k: len(v) for k, v in regions.items()}, "| genres:", len(genres_seen))


if __name__ == "__main__":
    main()
