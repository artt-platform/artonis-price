"""Generic crawler for the BidWizard / 'online-auctions' platform.

Used by several US regional auction houses with shared backend:
  - Everard Auctions and Appraisals    (auctions.everard.com)
  - Austin Auction Gallery             (bid.austinauction.com)

URL conventions (all hosts):
  Past catalogs index:  {host}/auctions/past
  Catalog index page:   {host}/auctions/{house-slug-or-id}/{slug}/catalog
  Lot detail:           {host}/online-auctions/{house}/{title-slug}-{numeric_id}

Both sites serve server-side HTML (no Playwright), and price markup is
the same — Estimate: $X - $Y plus a 'Height: by sight N in. x Width:
N in.' dimension block (when present).

Discovery filter — strict 2-pass:
  Pass 1: full-name keyword from data/vn_artist_catalog.py
          (slug-form, ≥ 6 chars, no family-elision)
  Pass 2: 'vietnamese' literal — catches anonymous/decorative lots and
          new artists not yet in catalog (logged for manual review).
"""
import re
import time
import sys
from pathlib import Path
import requests

from crawlers.common import insert_sale_result, log_crawl_run


# House → (label, host, default sale_location) — append a new entry to
# add another BidWizard-platform house.  No code change needed.
HOUSES = {
    "everard":         ("Everard Auctions and Appraisals", "https://auctions.everard.com",  "Savannah, GA, USA"),
    "austin_auction":  ("Austin Auction Gallery",          "https://bid.austinauction.com", "Austin, TX, USA"),
}

H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _load_vn_catalog():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data"))
    for m in list(sys.modules.keys()):
        if "vn_artist_catalog" in m:
            del sys.modules[m]
    from vn_artist_catalog import VN_ARTIST_CATALOG
    return VN_ARTIST_CATALOG


def _build_pass1_keywords(catalog):
    """Slug-form artist keywords from VN catalog (no family-elision).

    Family-elision creates short ambiguous slugs ('van-de', 'le-lam',
    'the-son') that collide with non-VN names.  Audit on Everard
    2026-06 found ~30 false positives this way — stick to full names.
    """
    kws = set()
    for normalized in catalog:
        if not normalized or len(normalized) < 5:
            continue
        slug = normalized.replace(" ", "-")
        if len(slug) >= 6:
            kws.add(slug)
    # Slug spellings outside the catalog normalisation
    kws.update(("lebadang", "le-thiet-cuong", "dao-hai-phong"))
    return kws


_PASS2_RE = re.compile(r"(?:^|-)(vietnamese?|viet-nam)(?:-|$)")
# Fake / attribution / copy markers — skip these lots up front.
_FAKE_MARKERS_RE = re.compile(
    r"(?:^|-)(?:after|attrib|attributed|d-apres|dapres|atelier|"
    r"ecole-de|cercle-de|entourage-de|copy|reproduction|signed-unknown)(?:-|$)"
)

_SIGHT_DIM_RE = re.compile(
    r"Height[^<\d]*by\s+sight[^<\d]*"
    r"(\d+(?:\s+\d+/\d+)?(?:\.\d+)?)\s*in\.?\s*[x×]\s*"
    r"Width[^<\d]*(\d+(?:\s+\d+/\d+)?(?:\.\d+)?)\s*in",
    re.IGNORECASE,
)
_EST_RE = re.compile(r"Estimate:\s*\$\s*([\d,]+)\s*[-–]\s*\$?\s*([\d,]+)", re.IGNORECASE)
_HAMMER_RE = re.compile(
    r"(?:Hammer|Realized|Sold for|Sale\s+price|Winning\s+bid)[^$]*\$\s*([\d,]+)",
    re.IGNORECASE,
)
# After a sale closes, Everard / BidWizard removes the 'Hammer' label
# and just shows the final price inside the bidding-area div.  Capture
# that explicitly — without this, every Everard lot after the sale-end
# date stays hammer=null even though the price is on the page.
_BIDDING_AREA_RE = re.compile(
    r'<div[^>]*class="[^"]*bidding-area[^"]*"[^>]*>\s*\$\s*([\d,]+)',
    re.IGNORECASE,
)


def _parse_frac_inches(s):
    """Convert '18 3/4', '27.5', or '18' inches to a float."""
    s = s.strip()
    m = re.match(r"^(\d+)\s+(\d+)/(\d+)$", s)
    if m:
        try:
            return float(m.group(1)) + float(m.group(2)) / float(m.group(3))
        except (ValueError, ZeroDivisionError):
            pass
    try:
        return float(s)
    except ValueError:
        return None


def list_past_catalogs(host, max_pages=10):
    """Return list of (house_slug, catalog_slug) tuples from /auctions/past."""
    cats = set()
    for p in range(1, max_pages + 1):
        url = host + "/auctions/past" + (f"?page={p}" if p > 1 else "")
        try:
            r = requests.get(url, headers=H, timeout=20)
            if r.status_code != 200:
                break
        except Exception:
            break
        found = set(re.findall(r"/auctions/([a-z0-9\-]+)/([a-z0-9\-]+-\d+)", r.text))
        new = found - cats
        if not new and p > 1:
            break
        cats |= found
        time.sleep(0.5)
    return sorted(cats)


def list_lot_slugs(host, house_slug, catalog_slug, max_pages=25):
    """Walk catalog index pages, return every /online-auctions/.../slug-id."""
    cat_url = f"{host}/auctions/{house_slug}/{catalog_slug}/catalog"
    slugs = set()
    for p in range(1, max_pages + 1):
        page_url = cat_url + (f"?page={p}" if p > 1 else "")
        try:
            r = requests.get(page_url, headers=H, timeout=25)
            if r.status_code != 200:
                break
        except Exception:
            break
        found = set(re.findall(
            r"/online-auctions/[a-z0-9\-]+/([a-z0-9\-]+-\d{6,})", r.text,
        ))
        new = found - slugs
        if not new:
            break
        slugs |= found
        time.sleep(0.3)
    return slugs, cat_url


def fetch_lot_detail(lot_url):
    """Return dict(title, dim_cm, medium, estimate_low/high, hammer)."""
    try:
        r = requests.get(lot_url, headers=H, timeout=25)
        if r.status_code != 200:
            return None
    except Exception:
        return None
    html = r.text
    out = {}
    m_t = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    if m_t:
        out["title"] = re.sub(r"\s+", " ", m_t.group(1)).strip()
    m_d = _SIGHT_DIM_RE.search(html)
    if m_d:
        h_in = _parse_frac_inches(m_d.group(1))
        w_in = _parse_frac_inches(m_d.group(2))
        if h_in is not None and w_in is not None:
            h_cm = round(h_in * 2.54, 1)
            w_cm = round(w_in * 2.54, 1)
            if 5 <= h_cm <= 500 and 5 <= w_cm <= 500:
                out["width_cm"] = w_cm
                out["height_cm"] = h_cm
    m_e = _EST_RE.search(html)
    if m_e:
        try:
            out["estimate_low"] = float(m_e.group(1).replace(",", ""))
            out["estimate_high"] = float(m_e.group(2).replace(",", ""))
        except ValueError:
            pass
    m_h = _HAMMER_RE.search(html) or _BIDDING_AREA_RE.search(html)
    if m_h:
        try:
            out["hammer"] = float(m_h.group(1).replace(",", ""))
        except ValueError:
            pass
    m_desc = re.search(r'<meta name="description" content="([^"]+)"', html)
    desc = (m_desc.group(1) if m_desc else "").lower()
    for kw in ("oil on canvas", "oil on silk", "oil on board", "oil on paper",
               "gouache on paper", "gouache on silk", "lacquer on wood",
               "lacquer painting", "ink on silk", "ink on paper",
               "watercolor on paper", "lithograph", "intaglio", "etching",
               "mixed media"):
        if kw in desc:
            out["medium"] = kw
            break
    # Sale date from catalog meta (best-effort)
    out["raw_desc"] = desc[:500]
    return out


def _match_artist(slug, vn_catalog, pass1_kws):
    """Find artist normalized_name for a slug. Returns key or None."""
    sl = slug.lower()
    for kw in pass1_kws:
        if re.search(r"(?:^|-)" + re.escape(kw) + r"(?:-|$)", sl):
            # 'lebadang' / 'dao-hai-phong' may not map cleanly — caller
            # resolves to artist row by aliases.
            return kw.replace("-", " ")
    return None


def _fmt(n):
    return f"{int(n)}" if abs(n - int(n)) < 0.01 else f"{n:.1f}"


def crawl_house(conn, house_key, artists_lookup, max_catalogs=None, delay=1.0, verbose=True):
    """Crawl one BidWizard-platform house.

    artists_lookup is a dict normalized_name → (id, display_name) built
    by the caller from the artists table.  No DB writes happen here for
    skipped lots — the FAKE_MARKERS gate and the VN-keyword filter both
    fire before insert.
    """
    label, host, default_loc = HOUSES[house_key]
    vn_catalog = _load_vn_catalog()
    pass1_kws = _build_pass1_keywords(vn_catalog)
    from datetime import datetime
    run_started = datetime.utcnow().isoformat() + "Z"

    cats = list_past_catalogs(host)
    if verbose:
        print(f"  [{house_key}] {len(cats)} past catalogs")
    if max_catalogs:
        cats = cats[:max_catalogs]

    total = 0
    for hs, cat_slug in cats:
        slugs, cat_url = list_lot_slugs(host, hs, cat_slug)
        cat_inserted = 0
        for slug in slugs:
            if _FAKE_MARKERS_RE.search(slug):
                continue
            p1_norm = _match_artist(slug, vn_catalog, pass1_kws)
            p2 = _PASS2_RE.search(slug.lower())
            if not p1_norm and not p2:
                continue
            # Resolve artist
            aid, aname = None, None
            if p1_norm:
                # Try direct lookup, then 'lebadang' / 'le ba dang' alias
                if p1_norm in artists_lookup:
                    aid, aname = artists_lookup[p1_norm]
                elif p1_norm == "lebadang":
                    for k, v in artists_lookup.items():
                        if "le ba dang" in k or "lebadang" in k:
                            aid, aname = v; break
            if not aid and p2:
                # Pass 2 anonymous — skip insert (would need manual review)
                continue
            if not aid:
                continue

            lot_url = f"{host}/online-auctions/{hs}/{slug}"
            data = fetch_lot_detail(lot_url)
            if not data:
                continue

            kind = "painting"
            if any(k in (data.get("medium") or data.get("title", "")).lower()
                   for k in ("lithograph", "intaglio", "etching", "screenprint")):
                kind = "print"

            rec = {
                "source": house_key,
                "source_url": lot_url,
                "sale_page_url": cat_url,
                "sale_location": default_loc,
                "auction_title": label,
                "artist_id": aid,
                "artist_name_raw": aname,
                "currency": "USD",
                "kind": kind,
            }
            if data.get("title"):
                rec["artwork_title"] = data["title"][:200]
            if data.get("medium"):
                rec["medium"] = data["medium"]
                ml = data["medium"]
                if "canvas" in ml: rec["support_type"] = "canvas"
                elif "silk" in ml: rec["support_type"] = "silk"
                elif "paper" in ml: rec["support_type"] = "paper"
                elif "wood" in ml or "lacquer" in ml:
                    rec["support_type"] = "lacquer" if "lacquer" in ml else "panel"
            if data.get("width_cm"):
                w, h = data["width_cm"], data["height_cm"]
                rec.update({
                    "width_cm": w, "height_cm": h,
                    "area_m2": round(w * h / 10000, 4),
                    "dimensions": f"{_fmt(w)} x {_fmt(h)} cm",
                })
            if data.get("estimate_low"):
                rec["estimate_low"] = data["estimate_low"]
                rec["estimate_high"] = data.get("estimate_high")
            if data.get("hammer"):
                rec["hammer_price"] = data["hammer"]
                rec["price_usd"] = data["hammer"]
                rec["price_with_premium_usd"] = round(data["hammer"] * 1.25, 2)
                rec["status"] = "sold"
                if data.get("width_cm"):
                    rec["price_per_m2_usd"] = round(
                        rec["price_with_premium_usd"] / rec["area_m2"], 2,
                    )
            else:
                rec["status"] = "estimate_only"

            try:
                insert_sale_result(conn, rec)
                cat_inserted += 1
            except Exception as e:
                if verbose:
                    print(f"    err {slug}: {e}")
            time.sleep(delay)
        if verbose and cat_inserted:
            print(f"  [{house_key}] {cat_slug[:60]}: {cat_inserted} inserted")
        total += cat_inserted
        conn.commit()
    log_crawl_run(conn, house_key, started_at=run_started, status="ok",
                  lots_inserted=total, note=label)
    return total


def crawl_everard(conn, **kw):
    return crawl_house(conn, "everard", _artists_lookup(conn), **kw)


def crawl_austin(conn, **kw):
    return crawl_house(conn, "austin_auction", _artists_lookup(conn), **kw)


def _artists_lookup(conn):
    """Build {normalized_name → (id, display_name)} from artists table.

    Accepts either a sqlite Connection or a SupabaseClient-like wrapper
    (caller passes whichever crawl_and_sync provides).  Falls back to
    direct REST GET so this crawler works in both modes.
    """
    import os
    URL = os.environ.get("SUPABASE_URL")
    KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if URL and KEY:
        try:
            r = requests.get(
                f"{URL}/rest/v1/artists?select=id,display_name,name,normalized_name",
                headers={"apikey": KEY, "Authorization": f"Bearer {KEY}",
                         "Range": "0-999"}, timeout=20,
            )
            data = r.json()
            return {
                a["normalized_name"]: (a["id"], a.get("display_name") or a["name"])
                for a in data if a.get("normalized_name")
            }
        except Exception:
            pass
    # SQLite fallback
    out = {}
    try:
        cur = conn.execute("SELECT id, name, normalized_name, display_name FROM artists")
        for aid, name, norm, disp in cur:
            if norm:
                out[norm] = (aid, disp or name)
    except Exception:
        pass
    return out


# Backward-compat / orchestrator entry points
def crawl_all(conn, **kw):
    """Default: crawl all registered houses on this platform."""
    n = 0
    for h in HOUSES:
        try:
            n += crawl_house(conn, h, _artists_lookup(conn), **kw)
        except Exception as e:
            print(f"  [{h}] error: {e}")
    return n
