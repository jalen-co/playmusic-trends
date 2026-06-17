#!/usr/bin/env python3
"""
PlayMusic Trend Pull — Apple Music RSS + Kworb (Spotify charts)
Two free sources, no auth, no Spotify app required.

  Apple  -> consumption-authority rank + genre tags
  Kworb  -> Spotify rank + MOVEMENT (position change, daily & 7-day stream deltas)

Outputs a merged, dated CSV where a track present on BOTH platforms and rising
is your strongest "surface it" signal.

Run:  python trend_pull.py --country us --limit 100
Deps: pip install requests pandas lxml
"""

import argparse
import datetime as dt
import io
import json
import re
import sys

import requests
import pandas as pd

APPLE_URL = "https://rss.applemarketingtools.com/api/v2/{country}/music/most-played/{limit}/songs.json"
KWORB_URL = "https://kworb.net/spotify/country/{country}_daily.html"
HEADERS = {"User-Agent": "PlayMusic-TrendPull/1.0 (internal ops)"}


def norm_key(artist: str, title: str) -> str:
    """Loose join key: lowercase, drop features/parens/punct so Apple & Spotify match."""
    s = f"{artist} {title}".lower()
    s = re.sub(r"\(feat\.?.*?\)|\bfeat\.?.*$|\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_apple(country: str, limit: int) -> pd.DataFrame:
    url = APPLE_URL.format(country=country, limit=limit)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    results = r.json()["feed"]["results"]
    rows = []
    for i, t in enumerate(results, start=1):
        genres = [g["name"] for g in t.get("genres", []) if g["name"] != "Music"]
        rows.append({
            "apple_rank": i,
            "artist": t["artistName"],
            "title": t["name"],
            "genre": genres[0] if genres else "",
            "apple_url": t.get("url", ""),
        })
    df = pd.DataFrame(rows)
    df["key"] = df.apply(lambda x: norm_key(x["artist"], x["title"]), axis=1)
    return df


def _to_int(x):
    try:
        return int(str(x).replace(",", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return None


def fetch_kworb(country: str) -> pd.DataFrame:
    url = KWORB_URL.format(country=country)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    # Wrap in StringIO — newer pandas rejects a raw HTML string here.
    tables = pd.read_html(io.StringIO(r.text))
    if not tables:
        raise RuntimeError(f"No tables found on Kworb page: {url}")
    # Pick the table that actually has the chart columns (don't assume index 0).
    table = None
    for t in tables:
        cols = [str(c).strip() for c in t.columns]
        if "Artist and Title" in cols or any("Artist" in c for c in cols):
            table = t
            break
    if table is None:
        table = tables[0]
    table.columns = [str(c).strip() for c in table.columns]
    if "Artist and Title" not in table.columns:
        raise RuntimeError(
            f"Kworb layout changed. Columns seen: {list(table.columns)}"
        )
    rows = []
    for _, row in table.iterrows():
        at = str(row.get("Artist and Title", "")).strip()
        if " - " not in at:
            continue
        artist, title = at.split(" - ", 1)  # artist/title separator is " - "
        rows.append({
            "spotify_rank": _to_int(row.get("Pos")),
            "spotify_pos_change": str(row.get("P+", "")).strip(),   # =, +N, -N (movement)
            "spotify_streams": _to_int(row.get("Streams")),
            "spotify_streams_delta": _to_int(row.get("Streams+")),
            "spotify_7day_delta": _to_int(row.get("7Day+")),
            "artist": artist.strip(),
            "title": title.strip(),
        })
    if not rows:
        raise RuntimeError(f"Parsed 0 rows from Kworb table at {url}")
    df = pd.DataFrame(rows)
    df["key"] = df.apply(lambda x: norm_key(x["artist"], x["title"]), axis=1)
    return df


def merge(apple: pd.DataFrame, kworb: pd.DataFrame) -> pd.DataFrame:
    m = pd.merge(
        apple[["key", "artist", "title", "genre", "apple_rank", "apple_url"]],
        kworb[["key", "artist", "title", "spotify_rank", "spotify_pos_change",
               "spotify_streams_delta", "spotify_7day_delta"]],
        on="key", how="outer", suffixes=("", "_k"),
    )
    # Keep a name for Spotify-only tracks (these are your "add" candidates).
    m["artist"] = m["artist"].combine_first(m["artist_k"])
    m["title"] = m["title"].combine_first(m["title_k"])
    m = m.drop(columns=["artist_k", "title_k"])
    m["on_both"] = m["apple_rank"].notna() & m["spotify_rank"].notna()
    # Rising = climbing on Spotify (positive position change) OR strong 7-day growth.
    def rising(v):
        pc = str(v["spotify_pos_change"])
        climbing = pc.startswith("+")
        return bool(climbing or (v["spotify_7day_delta"] or 0) > 0)
    m["rising"] = m.apply(rising, axis=1)
    # Crude surface-priority for sorting: cross-platform + rising floats to top.
    m["surface_priority"] = m["on_both"].astype(int) * 2 + m["rising"].astype(int)
    m = m.sort_values(
        ["surface_priority", "apple_rank", "spotify_rank"],
        ascending=[False, True, True],
    ).reset_index(drop=True)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="us", help="ISO alpha-2, e.g. us, gb, ca")
    ap.add_argument("--limit", type=int, default=100, help="Apple chart depth (max 100)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    try:
        apple = fetch_apple(args.country, args.limit)
    except Exception as e:
        sys.exit(f"APPLE fetch failed: {type(e).__name__}: {e}")
    try:
        kworb = fetch_kworb(args.country)
    except Exception as e:
        sys.exit(f"KWORB fetch failed: {type(e).__name__}: {e}")

    merged = merge(apple, kworb)
    stamp = dt.date.today().isoformat()
    out = args.out or f"trend_{args.country}_{stamp}.csv"
    merged.to_csv(out, index=False)

    # Dashboard-ready JSON (stable filename so the artifact always reads the latest).
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "country": args.country,
        "source_note": "Apple Music RSS (most-played) + Kworb (Spotify daily). Free, no auth.",
        "rows": merged.where(merged.notna(), None).to_dict(orient="records"),
    }
    with open("trend_latest.json", "w") as f:
        json.dump(payload, f, indent=2, default=str)

    both = merged[merged["on_both"]]
    rising_both = both[both["rising"]]
    print(f"Apple entries: {len(apple)} | Kworb entries: {len(kworb)} | merged: {len(merged)}")
    print(f"On BOTH platforms: {len(both)} | of those, rising: {len(rising_both)}")
    print(f"\nTop cross-platform risers (strongest surface candidates):")
    cols = ["artist", "title", "genre", "apple_rank", "spotify_rank",
            "spotify_pos_change", "spotify_7day_delta"]
    print(rising_both[cols].head(15).to_string(index=False))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
