#!/usr/bin/env python3
"""Scrape the archive.org GratefulDead collection into a flat JSONL of recordings.
Uses the cursor-based scraping API (handles >10k results). One pass, rate-limited."""
import urllib.request, urllib.parse, json, time, sys

ENDPOINT = "https://archive.org/services/search/v1/scrape"
FIELDS = "identifier,date,source,venue,coverage,avg_rating,num_reviews,downloads"
UA = {"User-Agent": "deadbase-gapfill/0.1 (homelab personal use)"}

def scrape(out_path):
    params = {"q": "collection:(GratefulDead)", "fields": FIELDS, "count": "1000"}
    cursor, total, n = None, None, 0
    with open(out_path, "w") as f:
        while True:
            p = dict(params)
            if cursor: p["cursor"] = cursor
            url = ENDPOINT + "?" + urllib.parse.urlencode(p)
            req = urllib.request.Request(url, headers=UA)
            d = json.load(urllib.request.urlopen(req, timeout=60))
            if total is None:
                total = d.get("total"); print(f"total recordings: {total}", file=sys.stderr)
            for item in d.get("items", []):
                f.write(json.dumps(item) + "\n"); n += 1
            cursor = d.get("cursor")
            print(f"  fetched {n}/{total}", file=sys.stderr)
            if not cursor: break
            time.sleep(0.5)  # be polite
    print(f"done: {n} recordings -> {out_path}", file=sys.stderr)
    return n

if __name__ == "__main__":
    scrape("archive_raw.jsonl")
