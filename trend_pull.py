#!/usr/bin/env python3
"""
PlayMusic Trend Monitor — pull (v6)
Built for the PlayMusic ops team.
Adds: Apple cross-reference (surface signal), genre momentum data.
"""

import datetime as dt
import io
import json
import re
import sys
import time
import html as html_mod

import requests
import pandas as pd

HEADERS = {"User-Agent": "PlayMusic-TrendMonitor/6.0 (internal ops)"}

KWORB = "https://kworb.net/spotify/country/{cc}_{period}.html"
APPLE_TOP = "https://rss.applemarketingtools.com/api/v2/{cc}/music/most-played/200/songs.json"
APPLE_GENRE = "https://itunes.apple.com/{cc}/rss/topsongs/limit=200/genre={gid}/json"

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
GENRE_MARKETS = ["us", "gb", "au", "ca", "de", "fr"]
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
    if table is None: raise RuntimeError(f"No chart {cc}/{period}")
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
    """Returns {norm_key: {"genre": str, "apple_rank": int}}"""
    r = requests.get(APPLE_TOP.format(cc=cc), headers=HEADERS, timeout=20)
    r.raise_for_status()
    out = {}
    for i, t in enumerate(r.json()["feed"]["results"], start=1):
        g = [x["name"] for x in t.get("genres", []) if x["name"] != "Music"]
        out[norm_key(t["artistName"], t["name"])] = {
            "genre": g[0] if g else "", "apple_rank": i}
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


def compute_surface(r):
    """Surface score: how strongly this song signals 'add to PlayMusic catalog'."""
    s = 0
    mv = r.get("move", "")
    if mv in ("NEW", "RE"): s += 3
    elif mv.startswith("+"):
        v = _to_int(mv)
        if v and v >= 5: s += 2
        elif v: s += 1
    if r.get("on_apple"): s += 2
    if r.get("rank", 999) <= 20: s += 2
    elif r.get("rank", 999) <= 50: s += 1
    return s


def compute_genre_momentum(data):
    """Per-genre: count of songs, how many rising vs falling."""
    # Use global daily as the reference chart.
    rows = data.get("global", {}).get("daily", [])
    if not rows:
        rows = data.get("us", {}).get("daily", [])
    genres = {}
    for r in rows:
        g = r.get("genre")
        if not g: continue
        if g not in genres:
            genres[g] = {"count": 0, "rising": 0, "falling": 0, "new": 0}
        genres[g]["count"] += 1
        mv = r.get("move", "")
        if mv in ("NEW", "RE"): genres[g]["new"] += 1
        elif mv.startswith("+"): genres[g]["rising"] += 1
        elif mv.startswith("-"): genres[g]["falling"] += 1
    # Compute net momentum.
    for g, v in genres.items():
        v["net"] = v["rising"] + v["new"] - v["falling"]
        total_moving = v["rising"] + v["falling"] + v["new"]
        v["heat"] = round(v["net"] / max(total_moving, 1), 2)
    return dict(sorted(genres.items(), key=lambda x: -x[1]["count"]))


def main():
    t0 = time.time()

    # 1. Spotify charts
    print("1/4 Spotify charts...")
    raw = {}
    for cc in MARKETS + ["global"]:
        for p in PERIODS:
            try: raw[(cc, p)] = fetch_kworb(cc, p)
            except Exception as e:
                print(f"  {cc}/{p} skipped: {e}")
                raw[(cc, p)] = []
            time.sleep(0.15)
    data = {}
    for cc in MARKETS + ["global"]:
        data[cc] = {p: raw.get((cc, p), []) for p in PERIODS}
    data["europe"] = {p: europe_aggregate(raw, p) for p in PERIODS}

    # 2. Apple cross-reference + genre tags
    print("2/4 Apple cross-reference...")
    apple = {}  # combined across storefronts
    for cc in GENRE_STOREFRONTS:
        try:
            am = fetch_apple_top(cc)
            for k, v in am.items():
                if k not in apple:
                    apple[k] = v
                elif not apple[k]["genre"] and v["genre"]:
                    apple[k]["genre"] = v["genre"]
        except Exception as e:
            print(f"  {cc} skipped: {e}")
        time.sleep(0.2)
    print(f"  apple tags: {len(apple)}")

    # Apply to chart rows.
    all_genres = set()
    for reg in data.values():
        for rows in reg.values():
            for r in rows:
                am = apple.get(r["key"])
                r["genre"] = (am["genre"] if am else "") or ""
                r["on_apple"] = am is not None
                r["apple_rank"] = am["apple_rank"] if am else None
                r["surface"] = compute_surface(r)
                if r["genre"]: all_genres.add(r["genre"])
                r.pop("key", None)

    # 3. Genre charts
    print("3/4 Genre charts (6 markets x 10 genres)...")
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
                print(f"  {gname}/{cc} skipped: {e}")
            time.sleep(0.25)
        for i, r in enumerate(combined, start=1):
            r["rank"] = i
            r.pop("key", None)
        genre_charts[gname] = combined
        if combined: all_genres.add(gname)

    # 4. Genre momentum
    print("4/4 Genre momentum...")
    momentum = compute_genre_momentum(data)

    # Save
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "regions": [r for r in REGION_ORDER if r in data],
        "region_labels": LABELS, "periods": PERIODS,
        "genres": sorted(all_genres), "genre_charts": genre_charts,
        "genre_momentum": momentum, "data": data,
    }
    json.dump(payload, open("trend_latest.json", "w"), separators=(",", ":"), default=str)

    chart_songs = sum(len(rows) for reg in data.values() for rows in reg.values())
    gc_total = sum(len(v) for v in genre_charts.values())
    elapsed = int(time.time() - t0)
    print(f"\nDone in {elapsed}s | charts: {chart_songs} | genre: {gc_total} | momentum genres: {len(momentum)}")


if __name__ == "__main__":
    main()
