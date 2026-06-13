"""Fetch upcoming Asian-art-relevant auctions from major houses.

Hits each house's public calendar/upcoming page and filters by keyword
("asia", "vietnam", "indochin", "modern art", "southeast"). Writes to
the upcoming_auctions table. Idempotent on sale_page_url.

Sources covered:
  - Christie's     /en/calendar
  - Sotheby's      /en/calendar
  - Aguttes        future sales (peintres-asie focus)
  - Bonhams        /auctions/upcoming
  - Phillips       /calendar
  - Drouot         /en/c/43/asian-art?status=future

Run:
  python3 supabase/fetch_upcoming.py
"""
import os, re, sys, json
from pathlib import Path
from datetime import datetime
import requests
import cloudscraper

ROOT = Path(__file__).resolve().parent.parent
ENV = {}
for line in (ROOT / ".env.local").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v
URL = ENV["SUPABASE_URL"]; KEY = ENV["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}",
     "Content-Type": "application/json",
     "Prefer": "resolution=merge-duplicates,return=minimal"}

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HEADERS = {"User-Agent": UA}

KEYWORDS = (
    "asia", "asian", "indochin", "southeast", "vietnam",
    "modern-and-contemporary", "modern-contemporary",
    "peintres-asie", "peintres-d-asie", "art-d-asie",
    "hong-kong", "hong-kong-modern",
)


def _has_keyword(url):
    low = url.lower()
    return any(k in low for k in KEYWORDS)


def _fetch(url):
    try:
        return requests.get(url, headers=HEADERS, timeout=20).text
    except Exception:
        return ""


def fetch_christies():
    """Christie's calendar — /en/calendar."""
    html = _fetch("https://www.christies.com/en/calendar")
    seen = set()
    out = []
    for m in re.finditer(r'href="(/en/auction/([a-z0-9\-]+))"[^>]*>([^<]{5,200})</a>', html):
        href, slug, label = m.group(1), m.group(2), m.group(3)
        if not _has_keyword(slug):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append({
            "source": "christies",
            "sale_page_url": "https://www.christies.com" + href,
            "auction_title": "Christie's — " + label.strip()[:120],
            "sale_date": "",  # date on calendar requires deeper parsing
            "sale_location": "",
        })
    return out


def fetch_sothebys():
    html = _fetch("https://www.sothebys.com/en/calendar")
    seen = set()
    out = []
    for m in re.finditer(r'href="(/en/buy/auction/(\d+)/([a-z0-9\-]+))"', html):
        href, year, slug = m.group(1), m.group(2), m.group(3)
        if not _has_keyword(slug):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append({
            "source": "sothebys",
            "sale_page_url": "https://www.sothebys.com" + href,
            "auction_title": "Sotheby's — " + slug.replace("-", " ").title()[:120],
            "sale_date": f"{year}-01-01",  # placeholder year; deep-fetch would refine
            "sale_location": "",
        })
    return out


def fetch_drouot():
    s = cloudscraper.create_scraper()
    try:
        html = s.get("https://drouot.com/en/c/43/asian-art?status=future", timeout=20).text
    except Exception:
        return []
    out = []
    seen = set()
    for m in re.finditer(r"/en/v/(\d+)-([a-z0-9\-]+)", html):
        sale_id, slug = m.group(1), m.group(2)
        url = f"https://drouot.com/en/v/{sale_id}-{slug}"
        if url in seen: continue
        seen.add(url)
        out.append({
            "source": "drouot",
            "sale_page_url": url,
            "auction_title": "Drouot — " + slug.replace("-", " ").title()[:120],
            "sale_date": "",
            "sale_location": "Paris",
        })
    return out[:20]


def fetch_aguttes():
    """Aguttes future Peintres d'Asie sales."""
    html = _fetch("https://www.aguttes.com/")
    out = []
    seen = set()
    for m in re.finditer(r'href="(/[a-z0-9\-/]+/peintres-?d-?asie[^"#]*)"', html, re.IGNORECASE):
        href = m.group(1)
        url = "https://www.aguttes.com" + href
        if url in seen: continue
        seen.add(url)
        out.append({
            "source": "aguttes",
            "sale_page_url": url,
            "auction_title": "Aguttes — Peintres d'Asie",
            "sale_date": "",
            "sale_location": "Neuilly-sur-Seine",
        })
    return out


def fetch_phillips():
    """Phillips upcoming sales (HK modern is the VN-relevant one)."""
    html = _fetch("https://www.phillips.com/auctions/upcoming-auctions")
    out = []
    seen = set()
    for m in re.finditer(r'href="(/auction/[A-Z0-9]+/[a-z0-9\-]+)"[^>]*>([^<]{5,200})</a>', html):
        href, label = m.group(1), m.group(2)
        if not _has_keyword(label.lower()) and not _has_keyword(href.lower()):
            continue
        if href in seen: continue
        seen.add(href)
        out.append({
            "source": "phillips",
            "sale_page_url": "https://www.phillips.com" + href,
            "auction_title": "Phillips — " + label.strip()[:120],
            "sale_date": "",
            "sale_location": "",
        })
    return out


def main():
    all_rows = []
    for fn in (fetch_christies, fetch_sothebys, fetch_drouot, fetch_aguttes, fetch_phillips):
        try:
            rows = fn()
            print(f"  {fn.__name__}: {len(rows)} rows", flush=True)
            all_rows.extend(rows)
        except Exception as e:
            print(f"  {fn.__name__}: ERROR {e}", flush=True)

    now_iso = datetime.utcnow().isoformat() + "Z"
    for r in all_rows:
        r["scraped_at"] = now_iso

    if not all_rows:
        print("No upcoming rows scraped.")
        return

    rsp = requests.post(
        f"{URL}/rest/v1/upcoming_auctions?on_conflict=sale_page_url",
        headers=H, json=all_rows, timeout=60,
    )
    print(f"\nUpsert {len(all_rows)} rows: HTTP {rsp.status_code}")
    if rsp.status_code not in (200, 201, 204):
        print(rsp.text[:300])


if __name__ == "__main__":
    main()
