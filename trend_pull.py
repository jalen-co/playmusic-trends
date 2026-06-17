#!/usr/bin/env python3
"""
PlayMusic Trend Pull v2 — free, no auth.

Per country, builds:
  - TRENDING: Spotify chart sorted by momentum (new entries + climbers + 7-day growth),
              cross-referenced with Apple presence.   Source: Kworb (Spotify daily).
  - GENRES:   top ~50 songs for each of ~10 genres.    Source: Apple legacy iTunes RSS.

Writes trend_latest.json for the dashboard.

Run:  python trend_pull.py
Deps: pip install requests pandas lxml
"""

import datetime as dt
import io
import json
import re
import sys
import time

import requests
import pandas as pd

HEADERS = {"User-Agent": "PlayMusic-TrendPull/2.0 (internal ops)"}

# Apple storefronts (real markets). "global" is Kworb-only (Apple has no global store).
APPLE_COUNTRIES = ["us", "gb", "ca", "au"]
KWORB_COUNTRIES = ["us", "gb", "ca", "au", "global"]

# Apple Music genre IDs (verified, stable across countries).
GENRES = {
    "Pop": 14, "Hip-Hop/Rap": 18, "Country": 6, "R&B/Soul": 15, "Dance": 17,
    "Electronic": 7, "Rock": 21, "Alternative": 20, "Latin": 12, "Reggae": 24,
}
GENRE_LIMIT = 50
KWORB_URL = "https://kworb.net/spotify/country/{cc}_daily.html"
APPLE_GENRE_URL = "https://itunes.apple.com/{cc}/rss/topsongs/limit={n}/genre={gid}/json"
APPLE_TOP_URL = "https://rss.applemarketingtools.com/api/v2/{cc}/music/most-played/100/songs.json"


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


# ---------- Apple: per-genre top songs (legacy iTunes RSS) ----------
def fetch_apple_genre(cc, gid, want_genre):
    url = APPLE_GENRE_URL.format(cc=cc, n=GENRE_LIMIT, gid=gid)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    entries = r.json().get("feed", {}).get("entry", [])
    if isinstance(entries, dict):          # single result comes back as a dict
        entries = [entries]
    rows = []
    for i, e in enumerate(entries, start=1):
        try:
            title = e["im:name"]["label"]
            artist = e["im:artist"]["label"]
        except (KeyError, TypeError):
            continue
        cat = (e.get("category") or {}).get("attributes", {})
        genre_name = cat.get("label") or cat.get("term") or want_genre
        rows.append({"rank": i, "title": title, "artist": artist, "genre": genre_name})
    return rows


# ---------- Apple: overall most-played (marketing RSS) ----------
def fetch_apple_top(cc):
    url = APPLE_TOP_URL.format(cc=cc)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    out = {}
    for i, t in enumerate(r.json()["feed"]["results"], start=1):
        g = [x["name"] for x in t.get("genres", []) if x["name"] != "Music"]
        out[norm_key(t["artistName"], t["name"])] = {"apple_rank": i, "genre": g[0] if g else ""}
    return out


# ---------- Kworb: Spotify daily chart with movement ----------
def fetch_kworb(cc):
    url = KWORB_URL.format(cc=cc)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    table = next((t for t in tables if any("Artist" in str(c) for c in t.columns)),
                 tables[0] if tables else None)
    if table is None:
        raise RuntimeError(f"No chart table at {url}")
    table.columns = [str(c).strip() for c in table.columns]
    rows = []
    for _, row in table.iterrows():
        at = str(row.get("Artist and Title", "")).strip()
        if " - " not in at:
            continue
        artist, title = at.split(" - ", 1)
        rows.append({
            "spotify_rank": _to_int(row.get("Pos")),
            "pos_change": str(row.get("P+", "")).strip(),
            "d7": _to_int(row.get("7Day+")),
            "artist": artist.strip(), "title": title.strip(),
        })
    return rows


def trend_score(r):
    pc = r["pos_change"]
    s = 0.0
    if pc in ("NEW", "RE"):
        s += 25
    elif pc.startswith("+"):
        s += min(_to_int(pc) or 0, 50) / 5.0      # up to 10
    elif pc.startswith("-"):
        s -= min(abs(_to_int(pc) or 0), 50) / 10.0
    if (r.get("d7") or 0) > 0:
        s += 5
    if r.get("on_both"):
        s += 5
    return round(s, 1)


def build_country(cc, want_apple):
    block = {"trending": [], "genres": {}}
    kworb = fetch_kworb(cc)
    apple_top = fetch_apple_top(cc) if want_apple else {}
    for r in kworb:
        k = norm_key(r["artist"], r["title"])
        match = apple_top.get(k)
        r["on_both"] = match is not None
        r["apple_rank"] = match["apple_rank"] if match else None
        if match and match["genre"]:
            r["genre"] = match["genre"]
        else:
            r.setdefault("genre", "")
        r["is_new"] = r["pos_change"] in ("NEW", "RE")
        r["score"] = trend_score(r)
    kworb.sort(key=lambda r: (r["is_new"], r["score"]), reverse=True)
    block["trending"] = kworb
    if want_apple:
        for name, gid in GENRES.items():
            try:
                block["genres"][name] = fetch_apple_genre(cc, gid, name)
            except Exception as e:
                print(f"  genre {name}/{cc} skipped: {type(e).__name__}: {e}")
            time.sleep(0.2)
    return block


def main():
    data, errors = {}, []
    for cc in KWORB_COUNTRIES:
        want_apple = cc in APPLE_COUNTRIES
        try:
            print(f"Building {cc}...")
            data[cc] = build_country(cc, want_apple)
        except Exception as e:
            msg = f"{cc} failed: {type(e).__name__}: {e}"
            print(msg)
            errors.append(msg)

    if not data:
        sys.exit("All countries failed: " + " | ".join(errors))

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "countries": list(data.keys()),
        "genres": list(GENRES.keys()),
        "source_note": "Apple iTunes RSS (genres) + Apple Marketing RSS (top) + Kworb Spotify (trending). Free, no auth.",
        "data": data,
    }
    with open("trend_latest.json", "w") as f:
        json.dump(payload, f, separators=(",", ":"), default=str)

    tot = sum(len(b["trending"]) for b in data.values())
    gtot = sum(len(v) for b in data.values() for v in b["genres"].values())
    print(f"\nDone. Countries: {list(data.keys())} | trending rows: {tot} | genre rows: {gtot}")
    if errors:
        print("Partial - some sources failed:\n  " + "\n  ".join(errors))


if __name__ == "__main__":
    main()
