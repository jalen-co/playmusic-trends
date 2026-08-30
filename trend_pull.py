#!/usr/bin/env python3
"""
PlayMusic Trend Monitor — pull (v10)
Adds Last.fm cultural signal: user-generated tags + listener/playcount data
for top-ranked and surface-pick songs.

Requires env var LASTFM_API_KEY (free, non-commercial: https://www.last.fm/api/account/create)
If missing, Last.fm enrichment is skipped gracefully — everything else still runs.
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

HEADERS = {"User-Agent": "PlayMusic-TrendMonitor/10.0 (internal ops)"}

KWORB = "https://kworb.net/spotify/country/{cc}_{period}.html"
APPLE_GENRE = "https://itunes.apple.com/{cc}/rss/topsongs/limit=200/genre={gid}/json"
ITUNES_TOP = "https://itunes.apple.com/{cc}/rss/topsongs/limit=200/json"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
LASTFM_KEY = os.environ.get("LASTFM_API_KEY", "")

PERIODS = ["daily", "weekly"]
MARKETS = ["us", "gb", "ca", "au", "de", "fr", "es", "it", "nl", "se",
           "br", "mx", "jp", "kr", "in"]
LABELS = {"global": "Global", "europe": "Europe", "us": "US", "gb": "UK",
          "ca": "Canada", "au": "Australia", "de": "Germany", "fr": "France",
          "es": "Spain", "it": "Italy", "nl": "Netherlands", "se": "Sweden",
          "br": "Brazil", "mx": "Mexico", "jp": "Japan", "kr": "South Korea",
          "in": "India"}
EU_AGG = ["gb", "de", "fr", "es", "it", "nl", "se"]
REGION_ORDER = ["global", "us", "europe", "gb", "ca", "au", "de", "fr", "es", "it",
                "nl", "se", "br", "mx", "jp", "kr", "in"]
GENRES = {
    "Pop": 14, "Hip-Hop/Rap": 18, "Country": 6, "R&B/Soul": 15,
    "Dance": 17, "Electronic": 7, "Rock": 21, "Alternative": 20,
    "Latin": 12, "K-Pop": 51, "Reggae": 24, "Singer/Songwriter": 10,
    "Soundtrack": 16, "J-Pop": 27,
}
GENRE_MARKETS = ["us", "gb", "au", "ca", "de", "fr", "es", "it", "nl", "se", "br", "mx"]
ITUNES_TAG_MARKETS = ["us", "gb", "au", "ca", "de", "fr", "es", "it", "br", "mx", "jp", "kr"]
RECENCY_DAYS = 730

# Last.fm enrichment budget — top-ranked + surface picks only, time-boxed.
LASTFM_RANK_CUTOFF = 50
LASTFM_MAX_LOOKUPS = 600
LASTFM_TIME_LIMIT = 360  # 6 minutes

GENRE_NORM = {
    "hard rock": "Rock", "soft rock": "Rock", "classic rock": "Rock", "indie rock": "Rock",
    "punk": "Rock", "metal": "Rock", "heavy metal": "Rock", "grunge": "Rock",
    "alternativa": "Alternative", "alt": "Alternative", "indie": "Alternative",
    "indie pop": "Alternative", "folk": "Singer/Songwriter",
    "urbano latino": "Latin", "reggaeton": "Latin", "música mexicana": "Latin",
    "musica mexicana": "Latin", "pop latino": "Latin", "latin pop": "Latin",
    "tropical": "Latin", "salsa": "Latin", "bachata": "Latin",
    "hip hop": "Hip-Hop/Rap", "hip-hop": "Hip-Hop/Rap", "rap": "Hip-Hop/Rap",
    "trap": "Hip-Hop/Rap", "hip hop/rap": "Hip-Hop/Rap",
    "edm": "Electronic", "house": "Electronic", "techno": "Electronic",
    "ambient": "Electronic", "trance": "Electronic", "dubstep": "Electronic",
    "electro": "Electronic",
    "r&b": "R&B/Soul", "rnb": "R&B/Soul", "soul": "R&B/Soul", "neo-soul": "R&B/Soul",
    "k-pop": "K-Pop", "kpop": "K-Pop", "korean pop": "K-Pop",
    "j-pop": "J-Pop", "jpop": "J-Pop", "anime": "J-Pop",
    "dance pop": "Dance", "disco": "Dance", "funk": "Dance",
    "country pop": "Country", "country rock": "Country", "americana": "Country",
    "dancehall": "Reggae", "ska": "Reggae", "afrobeats": "Reggae",
    "film": "Soundtrack", "tv": "Soundtrack", "musical": "Soundtrack",
    "singer-songwriter": "Singer/Songwriter", "cantautor": "Singer/Songwriter",
    "pop/rock": "Pop", "adult contemporary": "Pop", "teen pop": "Pop",
}
# Age-band fit — an EDITORIAL HEURISTIC for content curation, not verified listener
# demographic data. Weights reflect general genre culture/positioning, not survey data.
# Gender is intentionally not modeled: no reliable signal exists for it in this data,
# and inferring it from tags would just encode stereotypes (e.g. "female vocalists"
# describes the artist, not the audience).
GENRE_AGE_WEIGHTS = {
    "Soundtrack":         {"10-14": 60, "13-17": 30, "15-20": 10},
    "K-Pop":              {"10-14": 45, "13-17": 40, "15-20": 15},
    "J-Pop":              {"10-14": 45, "13-17": 40, "15-20": 15},
    "Pop":                {"10-14": 30, "13-17": 40, "15-20": 30},
    "Dance":              {"10-14": 15, "13-17": 40, "15-20": 45},
    "Latin":              {"10-14": 25, "13-17": 40, "15-20": 35},
    "Country":            {"10-14": 15, "13-17": 35, "15-20": 50},
    "Hip-Hop/Rap":        {"10-14": 10, "13-17": 40, "15-20": 50},
    "Reggae":             {"10-14": 15, "13-17": 30, "15-20": 55},
    "R&B/Soul":           {"10-14": 10, "13-17": 30, "15-20": 60},
    "Electronic":         {"10-14": 10, "13-17": 30, "15-20": 60},
    "Rock":               {"10-14": 10, "13-17": 30, "15-20": 60},
    "Alternative":        {"10-14": 10, "13-17": 30, "15-20": 60},
    "Singer/Songwriter":  {"10-14": 10, "13-17": 25, "15-20": 65},
}
YOUNGER_TAG_HINTS = {"disney", "kids", "anime", "cartoon", "children", "movie soundtrack", "family"}
OLDER_TAG_HINTS = {"explicit", "mature", "sexy", "party", "dark", "drinking", "adult"}


def compute_age_fit(genre, tags=None):
    base = GENRE_AGE_WEIGHTS.get(genre)
    if not base:
        return None
    scores = dict(base)
    for t in [t.lower() for t in (tags or [])]:
        if any(h in t for h in YOUNGER_TAG_HINTS):
            scores["10-14"] += 15
        if any(h in t for h in OLDER_TAG_HINTS):
            scores["15-20"] += 15
    band = max(scores, key=scores.get)
    total = sum(scores.values()) or 1
    return {"band": band, "confidence": round(scores[band] / total, 2)}
LASTFM_TAG_BLOCKLIST = {
    "seen live", "female vocalists", "male vocalists", "usa", "united states",
    "spotify", "under 2000 listeners", "beautiful", "awesome", "favorite",
    "favourites", "love", "00s", "90s", "80s", "70s",
}


def normalize_genre(g):
    if not g: return ""
    lower = g.lower().strip()
    if lower in GENRE_NORM: return GENRE_NORM[lower]
    for std in GENRES:
        if lower == std.lower(): return std
    return g


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


def parse_itunes_entries(data):
    entries = data.get("feed", {}).get("entry", [])
    if isinstance(entries, dict): entries = [entries]
    out = {}
    for e in entries:
        try:
            title = e["im:name"]["label"]
            artist = e["im:artist"]["label"]
        except (KeyError, TypeError): continue
        cat = (e.get("category") or {}).get("attributes", {})
        genre = normalize_genre(cat.get("label") or cat.get("term") or "")
        if genre:
            out.setdefault(norm_key(artist, title), genre)
    return out


def fetch_itunes_top_tags(cc):
    r = requests.get(ITUNES_TOP.format(cc=cc), headers=HEADERS, timeout=20)
    r.raise_for_status()
    return parse_itunes_entries(r.json())


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


def fetch_lastfm_track(artist, title):
    """Returns {"tags": [...], "listeners": int, "playcount": int} or None."""
    if not LASTFM_KEY:
        return None
    try:
        r = requests.get(LASTFM_API, headers=HEADERS, timeout=10, params={
            "method": "track.getInfo", "api_key": LASTFM_KEY,
            "artist": artist, "track": title, "format": "json", "autocorrect": "1"})
        if r.status_code != 200:
            return None
        d = r.json()
        t = d.get("track")
        if not t:
            return None
        listeners = _to_int(t.get("listeners"))
        playcount = _to_int(t.get("playcount"))
        raw_tags = t.get("toptags", {}).get("tag", [])
        if isinstance(raw_tags, dict):
            raw_tags = [raw_tags]
        tags = []
        for tg in raw_tags[:8]:
            name = tg.get("name", "").strip()
            if name and name.lower() not in LASTFM_TAG_BLOCKLIST:
                tags.append(name)
        return {"tags": tags[:5], "listeners": listeners, "playcount": playcount}
    except Exception:
        return None


def compute_surface(r):
    s = 0
    mv = r.get("move", "")
    if mv in ("NEW", "RE"): s += 3
    elif mv.startswith("+"):
        v = _to_int(mv)
        if v and v >= 10: s += 3
        elif v and v >= 5: s += 2
        elif v: s += 1
    if r.get("rank", 999) <= 10: s += 3
    elif r.get("rank", 999) <= 30: s += 2
    elif r.get("rank", 999) <= 50: s += 1
    if not r.get("genre"): s = max(0, s - 1)
    return s


def compute_genre_momentum(data):
    rows = data.get("global", {}).get("daily", [])
    if not rows: rows = data.get("us", {}).get("daily", [])
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
    for v in genres.values():
        v["net"] = v["rising"] + v["new"] - v["falling"]
        total = v["rising"] + v["falling"] + v["new"]
        v["heat"] = round(v["net"] / max(total, 1), 2)
    return dict(sorted(genres.items(), key=lambda x: -x[1]["count"]))


def main():
    t0 = time.time()

    # 1. Spotify charts
    print("1/5 Spotify charts...")
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
    print(f"  done ({int(time.time()-t0)}s)")

    # 2. Genre charts
    print("2/5 Genre charts (14 × 12)...")
    genre_charts = {}
    genre_tag_map = {}
    for gname, gid in GENRES.items():
        seen, combined = set(), []
        for cc in GENRE_MARKETS:
            try:
                for r in fetch_genre_chart(cc, gid, gname):
                    genre_tag_map.setdefault(r["key"], gname)
                    if r["key"] not in seen:
                        seen.add(r["key"])
                        combined.append(r)
            except Exception as e:
                print(f"  {gname}/{cc} skipped: {e}")
            time.sleep(0.2)
        for i, r in enumerate(combined, start=1):
            r["rank"] = i
            r.pop("key", None)
        genre_charts[gname] = combined
    print(f"  genre tags: {len(genre_tag_map)} ({int(time.time()-t0)}s)")

    # 3. iTunes general top songs (genre tag boost)
    print("3/5 iTunes top songs (genre tags)...")
    itunes_tags = {}
    for cc in ITUNES_TAG_MARKETS:
        try:
            for k, g in fetch_itunes_top_tags(cc).items():
                itunes_tags.setdefault(k, g)
        except Exception as e:
            print(f"  itunes {cc} skipped: {e}")
        time.sleep(0.2)
    print(f"  itunes tags: {len(itunes_tags)} ({int(time.time()-t0)}s)")

    # 4. Tag genre + compute surface on trending
    print("4/5 Tagging + scoring...")
    all_genres = set(genre_charts.keys())
    for reg in data.values():
        for rows in reg.values():
            for r in rows:
                k = r["key"]
                g = genre_tag_map.get(k) or itunes_tags.get(k) or ""
                r["genre"] = normalize_genre(g)
                r["surface"] = compute_surface(r)
                af = compute_age_fit(r["genre"])
                if af: r["age_fit"] = af
                if r["genre"]: all_genres.add(r["genre"])

    # 5. Last.fm enrichment — top-ranked + surface picks only
    print("5/5 Last.fm cultural signal...")
    if not LASTFM_KEY:
        print("  LASTFM_API_KEY not set — skipping (everything else still works)")
    lastfm_cache = {}
    if LASTFM_KEY:
        candidates = {}
        for reg in data.values():
            for rows in reg.values():
                for r in rows:
                    if r["rank"] <= LASTFM_RANK_CUTOFF or r.get("surface", 0) >= 3:
                        candidates[r["key"]] = (r["artist"], r["title"])
        candidates = list(candidates.items())
        print(f"  candidates: {len(candidates)}")
        lt0, done = time.time(), 0
        for k, (artist, title) in candidates:
            if done >= LASTFM_MAX_LOOKUPS or (time.time() - lt0) > LASTFM_TIME_LIMIT:
                print(f"  stopped at budget/time limit ({done} done)")
                break
            info = fetch_lastfm_track(artist, title)
            if info:
                lastfm_cache[k] = info
            done += 1
            time.sleep(0.25)
        print(f"  enriched: {len(lastfm_cache)}/{done} attempted ({int(time.time()-t0)}s)")

    for reg in data.values():
        for rows in reg.values():
            for r in rows:
                lf = lastfm_cache.get(r["key"])
                if lf:
                    r["tags"] = lf["tags"]
                    r["listeners"] = lf["listeners"]
                    r["playcount"] = lf["playcount"]
                    # Recompute with tag nudges now that tags are available.
                    af = compute_age_fit(r.get("genre", ""), lf["tags"])
                    if af: r["age_fit"] = af
                r.pop("key", None)

    momentum = compute_genre_momentum(data)

    all_songs = set()
    for reg in data.values():
        for rows in reg.values():
            for r in rows: all_songs.add((r["artist"], r["title"]))
    for songs in genre_charts.values():
        for s in songs: all_songs.add((s["artist"], s["title"]))

    tagged = sum(1 for reg in data.values() for rows in reg.values() for r in rows if r.get("genre"))
    total = sum(len(rows) for reg in data.values() for rows in reg.values())
    gc_total = sum(len(v) for v in genre_charts.values())

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "regions": [r for r in REGION_ORDER if r in data],
        "region_labels": LABELS, "periods": PERIODS,
        "genres": sorted(all_genres), "genre_charts": genre_charts,
        "genre_momentum": momentum, "data": data,
        "stats": {"unique_songs": len(all_songs), "genre_chart_songs": gc_total,
                  "trending_tagged": tagged, "trending_total": total,
                  "surface_picks": sum(1 for reg in data.values() for rows in reg.values()
                                       for r in rows if r.get("surface", 0) >= 3),
                  "lastfm_enriched": len(lastfm_cache)},
    }
    json.dump(payload, open("trend_latest.json", "w"), separators=(",", ":"), default=str)

    elapsed = int(time.time() - t0)
    print(f"\nDone in {elapsed}s")
    print(f"  Unique songs: {len(all_songs)} | Trending tagged: {tagged}/{total}")
    print(f"  Genre charts: {gc_total} | Last.fm enriched: {len(lastfm_cache)}")


if __name__ == "__main__":
    main()
