"""Re-fetch source URLs to populate sale_results.catalog_description.

Per-source fetchers extract the FULL catalog text (medium + signature
+ provenance + notes), not the regex-trimmed bits the original crawlers
stored.  Once populated, supabase/llm_extract_fields.py runs the LLM
pass to fill medium / year / signature_info / provenance properly.

Sources & strategy:
  bonhams       → direct API (api01.bonhams.com Typesense) — fast, no
                  auth.  Pull catalogDesc field directly.
  christies     → direct HTML — Cloudflare-light, plain requests works.
                  Pull <div class="lot-details">...</div>.
  sothebys      → __NEXT_DATA__ JSON, apolloCache.LotV2.description.
  millon        → meta description + <h3>/.sub-title block.
  aguttes/drouot/tajan/artcurial/osenat/gros_delettrez/le_auction
                → direct HTML; meta description usually carries the
                  whole catalog blob.
  invaluable    → Cloudflare-protected, requires Playwright.  We
                  reuse crawlers/invaluable_detail_parser.py's
                  full-page render.
  bidwizard (everard/austin_auction)
                → static HTML; meta description + Details block.

The fetcher is best-effort: lots that fail (404, timeout, blocked)
are logged and skipped.  Subsequent runs can retry.
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

# Add repo root to sys.path so we can import crawlers.* helpers
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _load_env():
    env_path = Path(__file__).resolve().parent.parent / ".env.local"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


_load_env()
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def _clean_html(s):
    """Strip HTML tags + collapse whitespace."""
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<style[^>]*>.*?</style>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&#?\w+;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# === Per-source fetchers ===

def fetch_christies(url: str) -> str:
    """Christie's lot page — pull lot-details block (medium + signature +
    dimensions + Painted in YYYY)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200: return ""
    except Exception:
        return ""
    # The lot-details block lives inside chr-accordion-item with the
    # catalog blob.  Pull the largest <span> that contains the artist
    # block + medium + Painted line.
    m = re.search(
        r'<chr-accordion-item[^>]*lot-details[^<]*>(.*?)</chr-accordion-item>',
        r.text, re.DOTALL,
    )
    text = m.group(1) if m else r.text
    cleaned = _clean_html(text)
    # The block usually leads with "ARTIST NAME (years)" — keep just from
    # there onward.
    m_start = re.search(r"\b[A-Z][A-Z\sÀ-Ÿ\-']+\s*\(\d{4}", cleaned)
    if m_start:
        cleaned = cleaned[m_start.start():]
    # Trim to first occurrence of accordion stop markers
    for stop in ("Conditions of Sale", "Notice of Sale", "Cookies preferences"):
        if stop in cleaned:
            cleaned = cleaned.split(stop)[0]
    return cleaned[:10000]


def fetch_bonhams(url: str) -> str:
    """Bonhams lot page — pull catalogDesc (description block)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200: return ""
    except Exception:
        return ""
    # The lot detail page has a meta description with the full catalog
    # blob, OR a <div class="lotDetailDescription">.
    m = re.search(r'<meta name="description" content="([^"]+)"', r.text)
    meta_desc = m.group(1) if m else ""
    m2 = re.search(
        r'<div[^>]*lotDetailDescription[^>]*>(.*?)</div>',
        r.text, re.DOTALL,
    )
    block = _clean_html(m2.group(1)) if m2 else ""
    # Prefer longer one
    return (block if len(block) > len(meta_desc) else meta_desc)[:10000]


def fetch_sothebys(url: str) -> str:
    """Sotheby's __NEXT_DATA__ apolloCache.LotV2.description."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200: return ""
    except Exception:
        return ""
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        r.text, re.DOTALL,
    )
    if not m: return ""
    try:
        data = json.loads(m.group(1))
    except Exception:
        return ""
    apollo = data.get("props", {}).get("pageProps", {}).get("apolloCache", {}) or {}
    lot = next((v for k, v in apollo.items() if k.startswith("LotV2:")), None)
    if not lot: return ""
    desc = lot.get("description", "") or ""
    prov = lot.get("provenance", "") or ""
    return _clean_html(desc + " " + prov)[:10000]


def fetch_millon(url: str) -> str:
    """Millon lot page — meta description + <h3>/.sub-title block."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200: return ""
    except Exception:
        return ""
    parts = []
    m = re.search(r'<meta name="description" content="([^"]+)"', r.text)
    if m: parts.append(m.group(1))
    m = re.search(r'<h3[^>]*>([^<]+)</h3>', r.text)
    if m: parts.append(m.group(1))
    # Adjugé + Estimation blocks
    for label in ("Adjugé", "Estimation"):
        m = re.search(
            rf'class="title">\s*{label}[^<]*</p>\s*<p\s+class="price">\s*([^<]+)</p>',
            r.text,
        )
        if m:
            parts.append(f"{label}: {m.group(1).strip()}")
    return " ".join(p.strip() for p in parts)[:10000]


def fetch_generic_meta(url: str) -> str:
    """Fallback — meta description + any visible <main>/<article> text."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200: return ""
    except Exception:
        return ""
    m = re.search(r'<meta name="description" content="([^"]+)"', r.text)
    meta_desc = m.group(1) if m else ""
    # Strip script/style; keep first 5000 chars of main content
    body = _clean_html(r.text)
    return (meta_desc + " " + body[:3000])[:10000]


SOURCE_FETCHERS = {
    "christies": fetch_christies,
    "bonhams": fetch_bonhams,
    "sothebys": fetch_sothebys,
    "millon": fetch_millon,
    "millon_vn": fetch_millon,
    "aguttes": fetch_generic_meta,
    "drouot": fetch_generic_meta,
    "tajan": fetch_generic_meta,
    "artcurial": fetch_generic_meta,
    "osenat": fetch_generic_meta,
    "gros_delettrez": fetch_generic_meta,
    "le_auction": fetch_generic_meta,
    "phillips": fetch_generic_meta,
    "everard": fetch_generic_meta,
    "austin_auction": fetch_generic_meta,
    "global_auction": fetch_generic_meta,
    "larasati": fetch_generic_meta,
    "ravenel": fetch_generic_meta,
    "heritage": fetch_generic_meta,
    # invaluable handled separately — needs Playwright
}


def _patch(lot_id: int, text: str) -> bool:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/sale_results?id=eq.{lot_id}",
        headers=SB_HEADERS,
        json={"catalog_description": text},
        timeout=30,
    )
    return r.status_code in (200, 204)


def backfill(source: str = None, limit: int = None, delay: float = 1.0, verbose: bool = True):
    """Re-fetch + populate catalog_description for lots missing it.

    Args:
      source:   limit to one source (e.g. 'bonhams').  None = all
                supported sources.
      limit:    cap on number of lots to process this run.
      delay:    seconds between fetches per source.
    """
    # Get lots needing backfill
    flt = "catalog_description=is.null&select=id,source,source_url&order=id"
    if source:
        flt = f"source=eq.{source}&" + flt
    rs = []
    off = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/sale_results?{flt}&offset={off}&limit=1000",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=30,
        )
        chunk = r.json()
        if not isinstance(chunk, list) or not chunk: break
        rs.extend(chunk)
        if limit and len(rs) >= limit:
            rs = rs[:limit]; break
        if len(chunk) < 1000: break
        off += 1000
    if verbose:
        print(f"Lots needing backfill: {len(rs)}")

    succeeded = failed = skipped = 0
    last_source = None
    for i, lot in enumerate(rs, 1):
        src = lot.get("source") or ""
        fetcher = SOURCE_FETCHERS.get(src)
        if not fetcher:
            skipped += 1
            continue
        url = lot.get("source_url") or ""
        if not url:
            skipped += 1
            continue
        try:
            text = fetcher(url)
        except Exception as e:
            failed += 1
            if verbose:
                print(f"  ✗ {lot['id']} ({src}): {e}")
            continue
        if not text or len(text) < 20:
            failed += 1
            continue
        if _patch(lot["id"], text):
            succeeded += 1
        else:
            failed += 1
        if verbose and i % 25 == 0:
            print(f"  ... {i}/{len(rs)}  ok={succeeded}  fail={failed}  skip={skipped}")
        time.sleep(delay)
    if verbose:
        print(f"\nDone.  ok={succeeded}  fail={failed}  skip={skipped}  total={len(rs)}")
    return {"ok": succeeded, "fail": failed, "skip": skipped, "total": len(rs)}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--source", help="filter to a single source key")
    p.add_argument("--limit", type=int, help="cap lots processed this run")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between fetches")
    args = p.parse_args()
    backfill(source=args.source, limit=args.limit, delay=args.delay)
