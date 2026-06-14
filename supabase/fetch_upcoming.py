"""Fetch upcoming Asian-art-relevant auctions from major houses.

Hits each house's public calendar/upcoming page and filters by keyword
("asia", "asian", "indochin", "vietnam", "modern", "contemporary",
"chinese", "japanese", "hong-kong", etc.). Writes to upcoming_auctions.
Idempotent on sale_page_url.

Sources covered (per probe 2026-06-14):
  - Christie's   /en/calendar               → 16 upcoming, ~4 asian
  - Sotheby's    /en/calendar               → 75 upcoming, 1-2 asian
  - Drouot       /en/auctions/future        → 37 upcoming, 2 asian
  - Aguttes      /ventes/prochaines-ventes  → JS-heavy, scan sitemap-fr fallback
  - Bonhams      /auctions/                 → 12 upcoming, asian only via dedicated cat
  - Phillips     /auctions/upcoming-auctions

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

# Keywords we treat as "Asian-relevant" — VN art often appears in:
#   - dedicated Asian-art sales
#   - "Modern & Contemporary" sales in HK / Paris (esp. Aguttes Peintres d'Asie)
#   - Indochinese-specific sales
KEYWORDS = (
    "asia", "asian", "indochin", "southeast", "vietnam",
    "peintres-asie", "peintres-d-asie", "art-d-asie", "arts-d-asie",
    "chinese", "japanese", "korean", "indian", "himalayan",
    "hong-kong",
    # Aguttes / Christie's modern sales often have Indochinese lots
    "tableaux-modernes", "modern-and-contemporary", "modern-contemporary",
    "asie", "inkspiration",
)


def _has_keyword(text):
    return any(k in text.lower() for k in KEYWORDS)


def _scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "darwin"},
    )


def _slug_title(slug):
    return slug.replace("-", " ").replace("_", " ").strip().title()


def fetch_christies():
    """Christie's calendar — /en/calendar.

    URL pattern: /en/auction/{slug}-{N}-{loc}, where loc∈{nyr,hgk,par,kls,…}.
    Strip the trailing -{N}-{loc} to recover a clean slug for filtering.
    """
    s = _scraper()
    try:
        html = s.get("https://www.christies.com/en/calendar", timeout=25).text
    except Exception:
        return []
    seen = set()
    out = []
    for m in re.finditer(r'/en/auction/([a-z0-9-]+-\d+-[a-z]+)', html):
        url_slug = m.group(1)
        # Strip "-24625-hgk" suffix for keyword test
        clean = re.sub(r'-\d+-[a-z]+$', '', url_slug)
        if not _has_keyword(clean):
            continue
        full = f"https://www.christies.com/en/auction/{url_slug}"
        if full in seen: continue
        seen.add(full)
        # Last 3 chars of slug suffix often encode location (hgk=hong kong, par=paris)
        loc_code = url_slug.rsplit('-', 1)[-1]
        loc_map = {
            "nyr": "New York", "hgk": "Hong Kong", "par": "Paris",
            "kls": "London King St", "lon": "London", "kls2": "London",
        }
        out.append({
            "source": "christies",
            "sale_page_url": full,
            "auction_title": "Christie's — " + _slug_title(clean)[:120],
            "sale_date": None,
            "sale_location": loc_map.get(loc_code),
        })
    return out


def fetch_sothebys():
    """Sotheby's calendar — /en/calendar.

    URL pattern: /en/buy/auction/{year}/{slug}. Keyword-filter on slug.
    """
    s = _scraper()
    try:
        html = s.get("https://www.sothebys.com/en/calendar", timeout=25).text
    except Exception:
        return []
    seen = set()
    out = []
    for m in re.finditer(r'/en/buy/auction/(\d{4})/([a-z0-9-]+)', html):
        year, slug = m.group(1), m.group(2)
        if not _has_keyword(slug):
            continue
        full = f"https://www.sothebys.com/en/buy/auction/{year}/{slug}"
        if full in seen: continue
        seen.add(full)
        out.append({
            "source": "sothebys",
            "sale_page_url": full,
            "auction_title": "Sotheby's — " + _slug_title(slug)[:120],
            "sale_date": None,
            "sale_location": None,
        })
    return out


def fetch_drouot():
    """Drouot future auctions — /en/auctions/future.

    URL pattern: /en/v/{id}-{slug}. The page lists future across all
    categories, so keyword-filter to recover the asian ones.
    """
    s = _scraper()
    try:
        html = s.get("https://drouot.com/en/auctions/future", timeout=25).text
    except Exception:
        return []
    seen = set()
    out = []
    for m in re.finditer(r'/en/v/(\d+)-([a-z0-9-]+)', html):
        sid, slug = m.group(1), m.group(2)
        if not _has_keyword(slug):
            continue
        full = f"https://drouot.com/en/v/{sid}-{slug}"
        if full in seen: continue
        seen.add(full)
        out.append({
            "source": "drouot",
            "sale_page_url": full,
            "auction_title": "Drouot — " + _slug_title(slug)[:120],
            "sale_date": None,
            "sale_location": "Paris",
        })
    return out


def fetch_aguttes():
    """Aguttes future Peintres d'Asie sales.

    Site is heavy SPA so direct HTML scrape misses links. Fallback:
    use the known Asian-catalog URL pattern via the artisio iframe page,
    which sometimes renders the catalog UUIDs server-side.
    """
    s = _scraper()
    found = set()
    # Try the artisio future-auctions endpoint
    for path in ("/ventes/prochaines-ventes", "/artisio"):
        try:
            html = s.get(f"https://www.aguttes.com{path}", timeout=25).text
        except Exception:
            continue
        # Match arts-dasie or peintres-d-asie catalog UUID URLs
        for m in re.finditer(
            r'/catalogue/((?:arts-?d-?asie|peintres-?d-?asie|tableaux-modernes)[-a-z0-9]*)',
            html,
        ):
            slug = m.group(1)
            full = f"https://www.aguttes.com/catalogue/{slug}"
            found.add(full)
    out = []
    for full in found:
        slug = full.rsplit("/", 1)[-1]
        # Strip UUID for readability
        clean = re.sub(r'-[0-9a-f]{8}-[0-9a-f-]+$', '', slug)
        out.append({
            "source": "aguttes",
            "sale_page_url": full,
            "auction_title": "Aguttes — " + _slug_title(clean)[:120],
            "sale_date": None,
            "sale_location": "Neuilly-sur-Seine",
        })
    return out


def fetch_bonhams():
    """Bonhams /auctions/ — only listing 12 main upcoming sales, no
    asian-dedicated upcoming during this probe. Returns [] when none
    pass keyword filter rather than scraping their /search endpoint
    (which requires API key)."""
    s = _scraper()
    try:
        html = s.get("https://www.bonhams.com/auctions/", timeout=25).text
    except Exception:
        return []
    seen = set()
    out = []
    for m in re.finditer(r'/auction/(\d+)/([a-z0-9-]+)', html):
        sid, slug = m.group(1), m.group(2)
        if not _has_keyword(slug):
            continue
        full = f"https://www.bonhams.com/auction/{sid}/{slug}/"
        if full in seen: continue
        seen.add(full)
        out.append({
            "source": "bonhams",
            "sale_page_url": full,
            "auction_title": "Bonhams — " + _slug_title(slug)[:120],
            "sale_date": None,
            "sale_location": None,
        })
    return out


def fetch_phillips():
    """Phillips upcoming sales — /auctions/upcoming-auctions."""
    s = _scraper()
    try:
        html = s.get("https://www.phillips.com/auctions/upcoming-auctions",
                     timeout=25).text
    except Exception:
        return []
    seen = set()
    out = []
    for m in re.finditer(r'/auction/([A-Z]{2}\d+)/([a-z0-9-]+)', html):
        sid, slug = m.group(1), m.group(2)
        if not _has_keyword(slug):
            continue
        full = f"https://www.phillips.com/auction/{sid}/{slug}"
        if full in seen: continue
        seen.add(full)
        out.append({
            "source": "phillips",
            "sale_page_url": full,
            "auction_title": "Phillips — " + _slug_title(slug)[:120],
            "sale_date": None,
            "sale_location": None,
        })
    return out


def _extract_sale_date(html):
    """Find earliest future YYYY-MM-DD on page. Prefers schema.org startDate."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    m_start = re.search(r'"startDate"\s*:\s*"(20\d{2}-\d{2}-\d{2})', html)
    if m_start:
        d = m_start.group(1)
        if d >= today:
            return d
    dates = sorted(set(re.findall(r'(20\d{2}-\d{2}-\d{2})', html)))
    future = [d for d in dates if d >= today]
    return future[0] if future else None


def enrich_sale_dates(rows):
    """For each row missing sale_date, fetch page and try to extract one.
    Skips on network error (date is optional)."""
    scraper = _scraper()
    for r in rows:
        if r.get("sale_date"):
            continue
        try:
            resp = scraper.get(r["sale_page_url"], timeout=20)
            if resp.status_code == 200:
                d = _extract_sale_date(resp.text)
                if d:
                    r["sale_date"] = d
        except Exception:
            pass


def prune_past_auctions():
    """Delete upcoming_auctions rows whose sale_date has passed (cleanup stale)."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    rsp = requests.delete(
        f"{URL}/rest/v1/upcoming_auctions?sale_date=lt.{today}",
        headers=H, timeout=30,
    )
    print(f"Prune past auctions (<{today}): HTTP {rsp.status_code}")


def main():
    fetchers = (
        ("christies", fetch_christies),
        ("sothebys",  fetch_sothebys),
        ("drouot",    fetch_drouot),
        ("aguttes",   fetch_aguttes),
        ("bonhams",   fetch_bonhams),
        ("phillips",  fetch_phillips),
    )
    all_rows = []
    for name, fn in fetchers:
        try:
            rows = fn()
            print(f"  {name:<10}: {len(rows)} asian-relevant", flush=True)
            for r in rows[:3]:
                print(f"    · {r['auction_title']}", flush=True)
            all_rows.extend(rows)
        except Exception as e:
            print(f"  {name:<10}: ERROR {e}", flush=True)

    if all_rows:
        print(f"\nEnriching {len(all_rows)} rows with sale_date…", flush=True)
        enrich_sale_dates(all_rows)
        with_date = sum(1 for r in all_rows if r.get("sale_date"))
        print(f"  → {with_date}/{len(all_rows)} got a sale_date", flush=True)

    now_iso = datetime.utcnow().isoformat() + "Z"
    for r in all_rows:
        r["scraped_at"] = now_iso

    if not all_rows:
        print("\nNo upcoming rows scraped.")
        prune_past_auctions()
        return

    rsp = requests.post(
        f"{URL}/rest/v1/upcoming_auctions?on_conflict=sale_page_url",
        headers=H, json=all_rows, timeout=60,
    )
    print(f"\nUpsert {len(all_rows)} rows: HTTP {rsp.status_code}")
    if rsp.status_code not in (200, 201, 204):
        print(rsp.text[:400])

    prune_past_auctions()


if __name__ == "__main__":
    main()
