"""Test that SOTHEBYS_COOKIE + INVALUABLE_COOKIE secrets work.

Triggered via the test_cookies.yml workflow (workflow_dispatch only).
Fetches 1 known lot from each source with the cookie attached and
prints the relevant HTML/JSON sections so we can see whether the
hammer is visible.

Sample lots:
  Sothebys HK: Le Pho "Two ladies" 2023-04-06 hammer HKD 5,080,000
    (operator verified manually)
  Invaluable: any lot we know has a hammer that's normally
    "Log in to view"

If the test prints the real hammer number → cookie works → we
build the real crawlers.  If not → cookie format wrong or domain
mismatched.
"""
from __future__ import annotations
import os, re, sys
import cloudscraper


def fetch(url: str, cookie: str | None, label: str) -> None:
    if not cookie:
        print(f"--- {label}: NO COOKIE in env ---")
        return
    # Strip leading / trailing whitespace + newlines that often sneak in
    # when pasting into GitHub Secrets.  Internal newlines are likewise
    # collapsed — a Cookie header can't contain raw newlines (HTTP spec).
    cookie = " ".join(cookie.split()).strip()
    print(f"--- {label}: fetching {url} ---")
    print(f"    cookie length: {len(cookie)} chars (cleaned)")
    s = cloudscraper.create_scraper(browser={'browser':'chrome','platform':'darwin','desktop':True})
    headers = {
        # Realistic Chrome on macOS — matches what the operator's
        # browser sent when copying the cookie (avoids UA mismatch
        # detection on Cloudflare-protected sites).
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cookie": cookie,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        r = s.get(url, headers=headers, timeout=30)
        r.encoding = "utf-8"
    except Exception as e:
        print(f"    fetch failed: {e}")
        return
    print(f"    HTTP {r.status_code}, body {len(r.text)} chars")

    # Look for any of these markers — sample, don't dump everything
    markers = [
        ("'Sold for' / 'Sold:'", r"(?:Sold for|Sold:)[^<\n]{0,80}"),
        ("'Log in to view'", r"(?:Log in to view|Sign in)[^<\n]{0,40}"),
        ("'Realised' / 'Realized'", r"Realis[ez]d[^<\n]{0,80}"),
        ("'Adjugé'", r"Adjug[eé][^<\n]{0,80}"),
        ("HKD / USD / EUR sums", r"(?:HKD|USD|EUR|GBP)\s*[\d,.]+"),
        ("sold:{__typename:ResultHidden}", r'sold[\\"]*:\s*\{\s*[\\"]*__typename[\\"]*:\s*[\\"]*ResultHidden'),
        ("sold:{amount}", r'sold[\\"]*:\s*\{[^}]*amount'),
        ("price in JSON", r'"price"\s*:\s*\d+'),
        ("hammerPrice in JSON", r'"hammerPrice"\s*:\s*\d+'),
        ("offer/price meta", r'<meta[^>]+(?:price|offer)[^>]+content="[^"]+"'),
    ]
    for name, pat in markers:
        hits = re.findall(pat, r.text, re.IGNORECASE)
        if hits:
            shown = hits[:5]
            print(f"  ✓ {name}: {len(hits)} hits — sample: {shown}")
        else:
            print(f"  ✗ {name}: 0")

    # Also look at <title>
    m = re.search(r'<title>([^<]+)</title>', r.text)
    if m:
        print(f"  <title>: {m.group(1)[:120]!r}")

    # If Sothebys → also try their GraphQL endpoint directly to see if
    # the same cookie unlocks the hammer there.  Hammer for HK lots
    # comes back from /api/getLotDetail or similar Apollo query.
    if "sothebys.com" in url and "lot/" not in url:
        # Try extracting the lot's internal ID from the page HTML
        m_lid = re.search(r'"lotId"\s*:\s*"([^"]+)"', r.text)
        if m_lid:
            print(f"  Sothebys lotId from page: {m_lid.group(1)}")


def main() -> None:
    sothebys_cookie = os.environ.get("SOTHEBYS_COOKIE", "")
    invaluable_cookie = os.environ.get("INVALUABLE_COOKIE", "")

    # Sothebys HK Le Pho "Two ladies" — we know real hammer is HKD 5,080,000
    fetch(
        "https://www.sothebys.com/en/buy/auction/2023/modern-day-auction/"
        "le-pho-li-pu-two-ladies-liang-wei-shi-nu",
        sothebys_cookie,
        "Sothebys HK Le Pho (real hammer HKD 5,080,000)",
    )
    print()

    # Invaluable — pick a known past lot from our DB
    # Bui Huu Hung "Mother and Children" — operator verified $6,500 USD
    fetch(
        "https://www.invaluable.com/auction-lot/"
        "bui-huu-hung-born-1957-mother-and-children-7640-c-e8b20b70cc",
        invaluable_cookie,
        "Invaluable Bui Huu Hung (real hammer $6,500 USD)",
    )


if __name__ == "__main__":
    main()
