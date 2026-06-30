"""Insert past Invaluable lots from a list of lot-detail URLs.

This replaces the ad-hoc inline scripts the operator was running
during URL paste sessions ("here are 20 Invaluable URLs for Lê
Phổ — import them").

The earlier ad-hoc code only saved the page meta `<title>` text
into `raw_snapshot` and left `catalog_description` NULL.  When
that title was just the artist name ("Le Thiet Cuong (b.1962)")
the LLM extractor had no signal and `medium` / `provenance` /
`year` stayed NULL — operator caught lot 19411 (Nguyễn Tư Nghiêm
'lacquer on panel', provenance line) and lot 558 (Vu Cao Dam Le
Salut, URL-slug leak into title) as fallout.

This script:
  1. Fetches the lot detail page via cloudscraper (the public
     access path Invaluable forgot to lock — see
     pull_invaluable_hammers.py for the same trick).
  2. Reads the JSON data island for the structured fields
     (lotName, soldAmount, isLotClosed, estimate range, image,
     etc.) AND
  3. Captures the richest description text it can find — JSON-LD
     `description`, the OpenGraph description, and the first
     prose-paragraph node — and concatenates them into one blob.
  4. Stores that blob in `catalog_description` (NOT just
     `raw_snapshot`) so the next `llm_extract_fields.py` run can
     extract medium / provenance / year.
  5. Inserts via UPSERT on source_url (idempotent — re-running
     the same URLs is a no-op).
  6. Skips lots whose page Invaluable returns 4xx / 5xx — those
     get logged and the operator can retry later after the CF
     cooldown.

Usage:
  python3 supabase/import_invaluable_urls.py URL [URL ...]
  python3 supabase/import_invaluable_urls.py < urls.txt
  echo URL1\\nURL2 | python3 supabase/import_invaluable_urls.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

try:
    import cloudscraper
except ImportError:
    print("install: pip install cloudscraper beautifulsoup4")
    sys.exit(1)

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawlers.common import (
    classify_kind,
    detect_support_type,
    to_usd,
)
from crawlers.parsers import extract_medium, parse_dim
from supabase.sync_protect import strip_authoritative, push_safe_status


def _load_env():
    p = ROOT / ".env.local"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


_load_env()
SU = os.environ["SUPABASE_URL"]
SK = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SB_R = {"apikey": SK, "Authorization": f"Bearer {SK}"}
SB_W = {**SB_R, "Content-Type": "application/json"}

PREMIUM_RATE = 1.25  # Invaluable houses average ~25%; some go higher.

# Patterns used to dig fields out of the Invaluable JSON data island.
# Invaluable inlines a large JSON blob alongside the page markup; the
# fields below come from that blob.  Tested 2026-06-28.
PAT_LOT_NAME = re.compile(r'"lotName"\s*:\s*"([^"]+)"')
PAT_LOT_TITLE = re.compile(r'"lotTitle"\s*:\s*"([^"]+)"')
PAT_SOLD = re.compile(r'"soldAmount"\s*:\s*(\d+(?:\.\d+)?)')
PAT_CLOSED = re.compile(r'"isLotClosed"\s*:\s*(true|false)')
PAT_EST_LOW = re.compile(r'"lowEstimate"\s*:\s*(\d+(?:\.\d+)?)')
PAT_EST_HIGH = re.compile(r'"highEstimate"\s*:\s*(\d+(?:\.\d+)?)')
PAT_CURRENCY = re.compile(r'"currencyCode"\s*:\s*"([A-Z]{3})"')
PAT_AUCTION_HOUSE = re.compile(r'"houseName"\s*:\s*"([^"]+)"')
PAT_AUCTION_LOCATION = re.compile(r'"location"\s*:\s*"([^"]+)"')
# og:description fallback — Invaluable always writes
#   "Bid now on Invaluable: <desc> from <HOUSE NAME> on <DATE> ..."
# so the bare 'from X on Y' run is a reliable second source when
# the JSON island doesn't carry houseName.  Operator 2026-06-29
# caught lots 31133/31134 stored as auction_title='Invaluable
# (via Invaluable)' because the JSON had no houseName and the
# importer fell back to the platform name.
PAT_HOUSE_FROM_OG = re.compile(
    r"from\s+([A-Z][A-Za-z0-9\s&,.\-'\"]+?)\s+on\s+(?:\w+\s+\d+,?\s*\d{4})"
)
PAT_SALE_DATE = re.compile(r'"saleDate"\s*:\s*"(\d{4}-\d{2}-\d{2})')
PAT_IMAGE_URL = re.compile(
    r'"(?:imageUrl|photoUrl)"\s*:\s*"(https?://[^"]+\.(?:jpg|jpeg|png))"',
    re.IGNORECASE,
)
PAT_LD_DESCRIPTION = re.compile(
    r'"@type"\s*:\s*"Product"[^{}]{0,500}"description"\s*:\s*"([^"]+)"',
    re.DOTALL,
)


def _clean_invaluable_title(title: str) -> str:
    """Strip Invaluable's metadata-bloated prefix and 'Untitled - X'
    wrapper from a lotName.  Two passes:

      1. Strip the artist + nationality + year prefix.  Many
         Invaluable lotNames lead with the artist's surname,
         nationality, and (birth) year before the actual artwork
         title — e.g. 'HUNG, VIETNAMESE 1957, UNTITLED - STANDING
         WOMAN' (lot 19624) where everything before 'UNTITLED' is
         metadata that belongs in artist_name_raw, not artwork_title.
         The prefix ends at the LAST comma that comes after either
         a year or a nationality token.

      2. Unwrap 'Untitled - X' / 'Untitled, X' / 'Untitled (X)' —
         when Invaluable's catalog records 'Untitled' as the
         primary title but parenthetically (or after a dash) gives
         the descriptive name the artist or estate uses, surface
         the descriptive name as the title.

    Output is title-cased so 'STANDING WOMAN' → 'Standing Woman'
    unless the input was already mixed-case (then preserve the
    user-facing capitalisation).
    """
    s = title.strip()
    # Pass 1 — prefix strip.  Find the END of the
    # 'NATIONALITY (year/years)' run and discard everything up to
    # and including the comma that follows it.
    m = re.search(
        r"(?i)\b(?:vietnamese|french|american|chinese|british|"
        r"frenchamerican|french[- ]american|french[- ]vietnamese)"
        r"(?:[\s,]+(?:born\s+)?\d{4}(?:\s*[-–—]\s*\d{4})?)?"
        r"\s*,\s*",
        s,
    )
    if m:
        s = s[m.end():].strip()
    else:
        # No nationality marker — try birth-death year pair, either
        # parenthesised ('Le Pho (1907-2001) Composition') or bare
        # ('Vu Cao Dam 1908-2000 The Black Horse Oil Painting').
        m2 = re.search(r"\(?\d{4}\s*[-–—]\s*\d{4}\)?\s*", s)
        if m2:
            s = s[m2.end():].strip()
    # Pass 1b — strip trailing medium / technique noise that some
    # houses append: 'The Black Horse Oil Painting' → 'The Black
    # Horse'.  Only strip if a substantial title (≥ 4 chars)
    # remains — operator 2026-06-28 caught the cleaner stripping
    # 'Mixed Media Painting' down to empty for lots whose actual
    # title IS just 'Mixed Media Painting' (Nguyen Gia Tri lots
    # 19238 + 19221 at Antique Arena, where the auction catalog
    # never gave a specific artwork title).
    s_stripped = re.sub(
        r"(?i)\s+(?:oil|watercolor|watercolour|gouache|ink|acrylic|"
        r"lacquer|tempera|pastel|mixed media|mixed medium)"
        r"(?:\s+(?:on|painting|drawing|sketch))?"
        r"(?:\s+(?:canvas|paper|panel|board|silk|wood|cardboard))?\s*$",
        "", s,
    ).strip(" ,-–—")
    if len(s_stripped) >= 4:
        s = s_stripped
    # Pass 2 — unwrap Untitled markers.
    m_u = re.match(
        r"(?i)^untitled\s*(?:[\-–—:,]+|\(\s*)\s*(.+?)(?:\s*\))?$", s
    )
    if m_u:
        s = m_u.group(1).strip()
    # Title-case ALL-CAPS strings; preserve mixed case as-is.
    if s and s == s.upper() and any(c.isalpha() for c in s):
        # Lower-case the small words after the first.
        small = {"a","an","and","of","the","in","on","with","to","at","for",
                 "du","de","la","le","et","aux","des","les"}
        words = s.split()
        s = " ".join(
            (w[:1].upper() + w[1:].lower())
            if (i == 0 or w.lower() not in small)
            else w.lower()
            for i, w in enumerate(words)
        )
    # Safeguard against garbage output.  When the strip removed
    # everything substantive and only metadata fragments remain
    # ('1942-2021)' / 'b.1962)' / '1965)') return empty so the
    # caller's metadata_signals check will flip it to NULL —
    # better '(không tên)' than a garbage tail.
    if (not s
            or len(s) < 3
            or re.match(r"^[\d.,()\s\-–—b]+$", s)
            or re.match(r"^(?:b\.|c\.|circa|\d{4})", s, re.I)):
        return ""
    return s


def fetch_lot_page(sc, url: str) -> tuple[str | None, int]:
    """Return (html, status_code).  None on transport error."""
    try:
        r = sc.get(url, timeout=20)
        return r.text, r.status_code
    except Exception as e:  # noqa: BLE001 — log + continue
        print(f"    ✗ fetch error: {type(e).__name__}: {e}")
        return None, 0


def extract_fields(html: str, url: str) -> dict | None:
    """Pull every structured field we can out of the Invaluable lot
    page.  Returns a dict ready to UPSERT, or None when the page
    looks too thin (no title + no description) to be a valid lot
    OR when the universal attribution gate fires (see SPEC §13:
    'Attributed to X' / 'School of X' / 'After X' / 'Manner of X' /
    'Circle of X' / 'Follower of X' / 'Studio of X' / 'd'après' /
    'atelier' are NOT confirmed works of the artist — they belong
    to a different market segment and must not be inserted under
    artist_id of X)."""
    soup = BeautifulSoup(html, "html.parser")

    # Title: prefer JSON lotName, fall back to lotTitle, then <title>.
    lot_name = (PAT_LOT_NAME.search(html) or [None, None])[1]
    lot_title = (PAT_LOT_TITLE.search(html) or [None, None])[1]
    page_title = (soup.title.get_text(strip=True) if soup.title else "")
    title = (lot_name or lot_title or page_title or "").strip()
    # Invaluable embeds escape sequences (é etc.); JSON-decode the
    # extracted strings so 'lê' renders correctly downstream.
    if title:
        try:
            title = json.loads(f'"{title}"')
        except json.JSONDecodeError:
            pass
    if not title:
        return None

    # Universal attribution gate.  Operator 2026-06-29 caught lot
    # 19314 ('Attributed to Bui Xuan Phai' from Invaluable Topwells)
    # surviving every other filter because em batch importer
    # bypassed the central crawlers.parsers.is_attribution check
    # that crawlers/invaluable.py runs.  Wire the same check here so
    # 'Attributed to' / 'After X' / 'Manner of' / 'School of' / etc.
    # lots are rejected at extract_fields() — they NEVER reach
    # upsert() and never get an artist_id attribution they don't
    # deserve.
    from crawlers.parsers import is_attribution
    if is_attribution(url, title):
        return None

    # Clean Invaluable's metadata-bloated titles.  The raw lotName
    # is often "ARTIST_NAME, NATIONALITY YYYY[-YYYY], REAL_TITLE" or
    # similar.  Operator 2026-06-28 caught lot 19624 'Bui Huu Bai
    # Lien Hung' surfacing as artwork_title 'HUNG, VIETNAMESE 1957,
    # UNTITLED - STANDING WOMAN' when the real title is just
    # 'Standing Woman'.  Strip the artist+nationality+year prefix
    # and the leading 'Untitled - ' / 'Untitled, ' marker so what
    # remains is the actual descriptive title.
    title = _clean_invaluable_title(title)

    # When ≥3 metadata signals remain (the prefix-strip didn't
    # leave any real title behind) — e.g. Ho Huu Thu lot 31116
    # where the WHOLE lotName was metadata — drop the title to
    # NULL so the UI surfaces it as '(không tên)'.
    metadata_signals = 0
    tl = title.lower()
    if re.search(r"\b(1[89]\d{2}|20\d{2})\s*[-–—]\s*(1[89]\d{2}|20\d{2})\b", title):
        metadata_signals += 1
    if re.search(r"\b(vietnamese|french|american|chinese|british)\b", tl):
        metadata_signals += 1
    if re.search(r"\b(oil|lacquer|gouache|watercol|ink|acryl|tempera|"
                 r"mixed medium|mixed media|pastel)\b", tl):
        metadata_signals += 1
    if re.search(r"\d+\s*(?:mm|cm)\s*(?:x|×|by)\s*\d+", tl):
        metadata_signals += 1
    if "with frame" in tl or "framed" in tl:
        metadata_signals += 1
    if metadata_signals >= 3:
        title = ""

    # Description: combine ALL text signals into one blob so the
    # downstream LLM extractor (llm_extract_fields.py) has the
    # widest possible context.  Order: JSON-LD description, OG
    # description, meta description, then the lot-detail panel's
    # first prose paragraph if present.
    desc_parts = []
    m_ld = PAT_LD_DESCRIPTION.search(html)
    if m_ld:
        try:
            desc_parts.append(json.loads(f'"{m_ld.group(1)}"'))
        except json.JSONDecodeError:
            desc_parts.append(m_ld.group(1))
    for sel in [
        'meta[property="og:description"]',
        'meta[name="description"]',
    ]:
        el = soup.select_one(sel)
        if el and el.get("content"):
            desc_parts.append(el["content"].strip())
    # First long-prose paragraph: Invaluable's lot-detail page
    # usually renders the catalog description as a <p> or <div>
    # somewhere below the bid panel.  Generic body scan: longest
    # paragraph with at least 50 chars that isn't a navigation /
    # footer block (filtered by parent class hints).
    long_p = ""
    for p in soup.find_all(["p", "div"]):
        cls = " ".join(p.get("class", [])).lower()
        if any(skip in cls for skip in ("nav", "footer", "header", "menu", "sidebar")):
            continue
        text = p.get_text(" ", strip=True)
        if 50 <= len(text) <= 2000 and len(text) > len(long_p):
            long_p = text
    if long_p:
        desc_parts.append(long_p)

    # De-dupe while preserving order: the meta + JSON-LD often
    # repeat the same string verbatim.
    seen = set()
    desc_unique = []
    for d in desc_parts:
        d = (d or "").strip()
        if d and d not in seen:
            seen.add(d)
            desc_unique.append(d)
    catalog_description = " | ".join(desc_unique)[:4000]

    # Numeric / status fields from the JSON island.
    sold = PAT_SOLD.search(html)
    closed = PAT_CLOSED.search(html)
    is_closed = bool(closed and closed.group(1) == "true")
    sold_amount = float(sold.group(1)) if sold else 0.0

    est_low = PAT_EST_LOW.search(html)
    est_high = PAT_EST_HIGH.search(html)
    currency = (PAT_CURRENCY.search(html) or [None, "USD"])[1]
    image_url = (PAT_IMAGE_URL.search(html) or [None, None])[1]
    house = (PAT_AUCTION_HOUSE.search(html) or [None, ""])[1]
    location = (PAT_AUCTION_LOCATION.search(html) or [None, ""])[1]
    sale_date = (PAT_SALE_DATE.search(html) or [None, ""])[1]
    # Fallback: parse 'from <HOUSE> on <DATE>' from og:description
    # when JSON-island houseName is missing.  Strip trailing
    # punctuation / auction-platform suffixes ('via Invaluable') so
    # the stored auction_title is the upstream house name only.
    if not house or house.strip().lower() == "invaluable":
        # Look in og:description / meta description first — they always
        # contain the 'from X on Y' line verbatim.
        for sel in ('meta[property="og:description"]',
                    'meta[name="description"]'):
            el = soup.select_one(sel)
            if el and el.get("content"):
                m_h = PAT_HOUSE_FROM_OG.search(el["content"])
                if m_h:
                    house = m_h.group(1).strip(" .,;:\"'")
                    # If the captured name ends with 'Auctions' /
                    # 'Auction' / 'Gallery' / 'Galleries' just leave
                    # it as-is — that's how the house brands itself.
                    break
    # If location still empty, default to the house name (better than
    # storing 'Invaluable' which is the aggregator, not a place).
    if not location or location.strip().lower() == "invaluable":
        location = house or location

    # Status / hammer.  Same logic as pull_invaluable_hammers.py
    # except em insert side: closed-with-sold → sold; closed-no-sold
    # → passed; not-closed → estimate_only.
    if is_closed and sold_amount > 0:
        status = "sold"
        hammer = sold_amount
    elif is_closed:
        status = "passed"
        hammer = None
    else:
        status = "estimate_only"
        hammer = None

    # Width / height — parse from the catalog description first
    # (richer text, more likely to include a 'X x Y cm' pair) and
    # fall back to the title.  parse_dim returns
    # (W, H, area_m2, display_str) — Invaluable convention is
    # W × H so don't pass a source key.
    width_cm, height_cm, _area_m2, dim_str = parse_dim(
        catalog_description or title, source=""
    )
    # Image-aspect orientation cross-check (root fix).  Invaluable
    # mirrors many US regional houses whose catalog writers swap
    # W↔H inconsistently (see lots 19238/19221: same artist, same
    # series, same dims, opposite orientation in DB).  Don't trust
    # the parser's guess — fetch the lot photo, measure its aspect,
    # and swap W/H to match the image when they conflict.  Same
    # logic as supabase/fix_dim_orientation.py inlined here so the
    # row is correct on FIRST insert, not after cron's next pass.
    #
    # Skip when either the dims or the image are near-square
    # (< 10 % off square) — too noisy to decide a swap.
    if width_cm and height_cm and image_url:
        try:
            from PIL import Image
            import io as _io
            ir = requests.get(image_url, timeout=12)
            if ir.status_code == 200:
                img = Image.open(_io.BytesIO(ir.content))
                iw, ih = img.size
                dim_ratio = max(width_cm, height_cm) / min(width_cm, height_cm)
                img_ratio = max(iw, ih) / min(iw, ih)
                # Both must be decisively non-square AND disagree
                if dim_ratio > 1.10 and img_ratio > 1.10:
                    dim_is_landscape = width_cm > height_cm
                    img_is_landscape = iw > ih
                    if dim_is_landscape != img_is_landscape:
                        width_cm, height_cm = height_cm, width_cm
                        dim_str = f"{width_cm:g} x {height_cm:g} cm"
        except Exception:
            # Image fetch / decode errors are non-fatal — orientation
            # still gets caught by the cron pass over fix_dim_orientation.
            pass

    # Medium / support_type via the existing parser stack.  When the
    # description has 'oil on canvas' / 'lacquer on panel' / etc.,
    # this fills both fields — no LLM call needed.  When it
    # doesn't, leave NULL and let llm_extract_fields.py handle it
    # on the next cron tick.
    medium = extract_medium(catalog_description) or extract_medium(title) or ""
    support_type = detect_support_type(medium, title) if medium else None
    kind = classify_kind(medium, title, catalog_description, dim_str)

    # USD conversion.
    price_usd, _ = to_usd(hammer, currency) if hammer else (None, None)
    premium = round(hammer * PREMIUM_RATE, 2) if hammer else None
    premium_usd, _ = to_usd(premium, currency) if premium else (None, None)

    return {
        "source": "invaluable",
        "source_url": url,
        "artwork_title": title[:300],
        "catalog_description": catalog_description or None,
        # raw_snapshot kept for legacy consumers (and as a backup
        # when catalog_description gets re-derived later).
        "raw_snapshot": (catalog_description or title)[:500],
        "medium": medium or None,
        "support_type": support_type,
        "kind": kind,
        "width_cm": width_cm,
        "height_cm": height_cm,
        "dimensions": dim_str or None,
        "estimate_low": float(est_low.group(1)) if est_low else None,
        "estimate_high": float(est_high.group(1)) if est_high else None,
        "currency": currency,
        "hammer_price": hammer,
        "price_usd": round(price_usd, 2) if price_usd else None,
        "price_with_premium": premium,
        "price_with_premium_usd": round(premium_usd, 2) if premium_usd else None,
        "status": status,
        "auction_title": house or None,
        "sale_location": location or None,
        "sale_date": sale_date or None,
        "image_url": image_url,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def upsert(rec: dict) -> tuple[bool, str]:
    """UPSERT on source_url.  Returns (success, info).

    Routes through strip_authoritative + push_safe_status so this
    script's UPSERTs follow the same protection rules as the cron
    sync — re-importing an old URL whose hammer was already filled
    in by pull_invaluable_hammers.py must NOT overwrite that hammer
    with NULL just because Invaluable's lot-detail JSON dropped the
    soldAmount field over time.  Operator 2026-06-28 caught lot
    31095 going from sold (real hammer) back to estimate_only
    (hammer=NULL) on the first test run because of exactly this.

    EXCEPTION: when extract_fields explicitly set artwork_title=""
    (the metadata-only heuristic — Ho Huu Thu lot 31116 had its
    'title' = artist+nationality+years+medium+dim metadata) we
    DO want to overwrite the existing bad title with empty so the
    UI surfaces the row as '(không tên)'.  Send it as null so
    PostgREST clears the column instead of leaving the previous
    metadata string in place.
    """
    # Operator 2026-06-29: REMOVED the explicit_clear_title=None
    # path.  When em cleaner can't produce a usable title for an
    # existing row, do NOT overwrite the row's current title with
    # NULL — that wiped 'Chevaux Orientaux' (lot 31133) and 'Le
    # Cavalier' (lot 31134) just because the Invaluable lotName
    # was a full descriptive run instead of a quoted title.  When
    # artwork_title is "" / None, let strip_authoritative drop the
    # key so PostgREST preserves whatever Supabase already holds.
    strip_authoritative(rec)
    push_safe_status(rec)
    r = requests.post(
        f"{SU}/rest/v1/sale_results?on_conflict=source_url",
        headers={**SB_W, "Prefer": "resolution=merge-duplicates"},
        json=rec, timeout=20,
    )
    return r.status_code in (200, 201, 204), \
        (r.text[:200] if r.status_code >= 300 else "ok")


def _find_existing_direct_match(rec: dict) -> int | None:
    """Look for an EXISTING row from a DIRECT (non-Invaluable) crawler
    that matches this Invaluable lot.  Used to skip the Invaluable
    mirror when em already have the authoritative direct version.

    Match criteria (all required):
      - Same sale_date
      - Same dimensions string
      - Same artist (by artist_id when both are linked, OR same
        artist_name_raw lower-cased)
      - Either same artwork_title OR image_phash within 10/64 Hamming

    Operator 2026-06-30 caught lot 31058 (Invaluable mirror of Do
    Quang Em 'Mother and Child' Bonhams 2025-03-31) sitting in DB
    alongside the authoritative Bonhams direct lot 844 — same
    artwork, two rows, polluting cluster counts.  Insert-time dedup
    here prevents the mirror from ever landing.
    """
    sale_date = rec.get("sale_date")
    dim = rec.get("dimensions")
    title = (rec.get("artwork_title") or "").strip()
    phash = rec.get("image_phash")
    if not sale_date or not dim:
        return None
    params = {
        "sale_date": f"eq.{sale_date}",
        "dimensions": f"eq.{dim}",
        "source": "neq.invaluable",  # only direct sources count as auth.
        "select": "id,artwork_title,image_phash,artist_name_raw",
        "limit": "20",
    }
    if rec.get("artist_name_raw"):
        params["artist_name_raw"] = f"ilike.*{rec['artist_name_raw'][:30]}*"
    try:
        r = requests.get(f"{SU}/rest/v1/sale_results",
                         params=params, headers=SB_R, timeout=15)
        if r.status_code != 200:
            return None
        candidates = r.json()
    except Exception:  # noqa: BLE001
        return None
    title_norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    for c in candidates:
        c_title = (c.get("artwork_title") or "")
        c_title_norm = re.sub(r"[^a-z0-9]+", " ", c_title.lower()).strip()
        # Title overlap test — one contains the other (handles
        # Invaluable's metadata-bloated title "1942-2021) Mother and
        # Child" vs the Bonhams direct "Mother and Child").
        if title_norm and c_title_norm:
            if title_norm in c_title_norm or c_title_norm in title_norm:
                return c["id"]
        # Image-hash fallback (Hamming ≤ 10 / 64 bits, same as
        # /admin/duplicates threshold).
        if phash and c.get("image_phash"):
            try:
                a = int(phash, 16)
                b = int(c["image_phash"], 16)
                if bin(a ^ b).count("1") <= 10:
                    return c["id"]
            except ValueError:
                pass
    return None


def main():
    if len(sys.argv) > 1:
        urls = [u for u in sys.argv[1:] if u.startswith("http")]
    else:
        urls = [
            line.strip() for line in sys.stdin
            if line.strip().startswith("http")
        ]
    if not urls:
        print("no URLs.  pass on argv or via stdin.")
        sys.exit(1)

    sc = cloudscraper.create_scraper()
    inserted = err = skipped = 0
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        html, code = fetch_lot_page(sc, url)
        if not html or code != 200:
            err += 1
            print(f"    ✗ HTTP {code}")
            continue
        rec = extract_fields(html, url)
        if rec:
            # Insert-time dedup: skip when the same artwork already
            # exists as a direct-house row.  See _find_existing_direct_match
            # for criteria.  Operator 2026-06-30 — Do Quang Em
            # 'Mother and Child' Bonhams 2025-03-31 ended up as TWO
            # rows (Bonhams direct lot 844 + Invaluable mirror lot
            # 31058) because no dedup gate fired at insert.
            direct_id = _find_existing_direct_match(rec)
            if direct_id:
                print(f"    ⊘ DUP: skipping Invaluable mirror — direct "
                      f"crawler already owns lot {direct_id}")
                skipped += 1
                continue
        if not rec:
            skipped += 1
            print("    ✗ no usable title")
            continue
        # Capture status for logging BEFORE upsert() runs
        # push_safe_status, which pops the key when it's provisional
        # (estimate_only/unknown) to avoid stomping a Supabase-side
        # sold row.
        log_status = rec.get("status", "?")
        ok, info = upsert(rec)
        if ok:
            inserted += 1
            tag = (
                "SOLD" if log_status == "sold"
                else "PASSED" if log_status == "passed"
                else "estimate_only"
            )
            log_title = rec.get("artwork_title") or "(không tên)"
            print(
                f"    ✓ {tag} | {log_title[:60]!s:60} "
                f"| medium={rec.get('medium') or '-'} kind={rec.get('kind', '?')}"
            )
        else:
            err += 1
            print(f"    ✗ upsert: {info}")
        # Light pacing — Invaluable's CF starts blocking after
        # ~50 requests per session.
        time.sleep(0.4)

    print(f"\nDone.  inserted/updated={inserted}  errors={err}  skipped={skipped}")


if __name__ == "__main__":
    main()
