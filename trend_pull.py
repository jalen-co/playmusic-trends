#!/usr/bin/env python3
"""
PlayMusic Trend Monitor — pull (v4.2)
No iTunes Search lookups. Genre tags from Apple most-played only (fast).
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

HEADERS = {"User-Agent": "PlayMusic-TrendMonitor/4.2 (internal ops)"}

KWORB = "https://kworb.net/spotify/country/{cc}_{period}.html"
APPLE_TOP = "https://rss.applemarketingtools.com/api/v2/{cc}/music/most-played/200/songs.json"
APPLE_GENRE = "https://itunes.apple.com/{cc}/rss/topsongs/limit=100/genre={gid}/json"

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


def norm_key(artist, title):
    s = f"{artist} {title}".lower()
    s = html_mod.unescape(s)
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = re.sub(r"\(feat\.?.*?\)|\bfeat\.?.*$|\(.*?\)|\[.*?\]|\(w/.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_text(s):
    if not s: return s
    s = html_mod.unescape(s)
    s = re.sub(r'â[\x80-\xbf][\x80-\xbf]?', "'", s)
    return s.strip()


def _to_int(x):
    try: return int(str(x).replace(",", "").replace("+", "").strip())
    except (ValueError, TypeError): return None


def fetch_kworb(cc, period):
    r = requests.get(KWORB.format(cc=cc, period=period), headers=HEADERS, timeout=20)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    table = next((t for t in tables if any("Artist" in str(c) for c in t.columns)),
                 tables[0] if tables else None)
    if table is None: raise RuntimeError(f"No chart table {cc}/{period}")
    table.columns = [str(c).strip() for c in table.columns]
    rows = []
    for _, row in table.iterrows():
        at = str(row.get("Artist and Title", "")).strip()
        if " - " not in at: continue
        artist, title = at.split(" - ", 1)
        rk = _to_int(row.get("Pos"))
        if not rk: continue
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
    r = requests.get(APPLE_TOP.format(cc=cc), headers=HEADERS, timeout=20)
    r.raise_for_status()
    out = {}
    for t in r.json()["feed"]["results"]:
        g = [x["name"] for x in t.get("genres", []) if x["name"] != "Music"]
        out[norm_key(t["artistName"], t["name"])] = g[0] if g else ""
    return out


def fetch_genre_chart(cc, gid, genre_name):
    r = requests.get(APPLE_GENRE.format(cc=cc, gid=gid), headers=HEADERS, timeout=20)
    r.raise_for_status()
    entries = r.json().get("feed", {}).get("entry", [])
    if isinstance(entries, dict): entries = [entries]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RECENCY_DAYS)
    rows = []
    for e in entries:
        try:
            title = e["im:name"]["label"]
            artist = e["im:artist"]["label"]
        except (KeyError, TypeError): continue
        rd = e.get("im:releaseDate", {}).get("label", "")
        if rd:
            try:
                if dt.datetime.fromisoformat(rd.replace("Z", "+00:00")) < cutoff: continue
            except (ValueError, TypeError): pass
        rows.append({"title": clean_text(title), "artist": clean_text(artist),
                     "genre": genre_name, "key": norm_key(artist, title)})
    return rows


def main():
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

    print("Building genre map (Apple most-played)...")
    gmap = {}
    for cc in GENRE_STOREFRONTS:
        try:
            gm = fetch_apple_top(cc)
            for k, g in gm.items():
                if g and k not in gmap: gmap[k] = g
        except Exception as e:
            print(f"  {cc} skipped: {type(e).__name__}: {e}")
        time.sleep(0.2)
    print(f"  tags: {len(gmap)}")

    all_genres = set()
    for reg in data.values():
        for rows in reg.values():
            for r in rows:
                r["genre"] = gmap.get(r["key"], "")
                if r["genre"]: all_genres.add(r["genre"])
                r.pop("key", None)

    print("Building per-genre charts...")
    genre_charts = {}
    for gname, gid in GENRES.items():
        seen, combined = set(), []
        for cc in GENRE_MARKETS:
            try:
                for r in fetch_genre_chart(cc, gid, gname):
                    if r["key"] not in seen:
                        seen.add(r["key"])
                        combined.append(r)
            except Exception as e:
                print(f"  {gname}/{cc} skipped: {type(e).__name__}: {e}")
            time.sleep(0.3)
        for i, r in enumerate(combined, start=1):
            r["rank"] = i
            r.pop("key", None)
        genre_charts[gname] = combined
        if combined: all_genres.add(gname)
    print(f"  sizes: { {g: len(v) for g, v in genre_charts.items()} }")

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "regions": [r for r in REGION_ORDER if r in data],
        "region_labels": LABELS, "periods": PERIODS,
        "genres": sorted(all_genres), "genre_charts": genre_charts, "data": data,
    }
    json.dump(payload, open("trend_latest.json", "w"), separators=(",", ":"), default=str)
    tagged = sum(1 for reg in data.values() for rows in reg.values() for r in rows if r.get("genre"))
    total = sum(len(rows) for reg in data.values() for rows in reg.values())
    print(f"\nDone. regions: {len(data)} | songs: {total} | tagged: {tagged}/{total}")


if __name__ == "__main__":
    main()
