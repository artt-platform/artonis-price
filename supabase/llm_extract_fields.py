"""Run crawlers/llm_parser.py against sale_results.catalog_description
and patch the structured fields (medium, year, signature_info,
provenance, dimensions_text, title, language, confidence).

Designed for both backfill (one-shot over historical data) and
incremental (recently-inserted lots without LLM enrichment yet).

Run-time guards:
  - Skip lots that already have a non-trivial medium AND year — saves
    cost on already-clean rows.  Override with --refresh.
  - Skip lots with catalog_description shorter than 30 chars — too
    little signal, LLM would just hallucinate.
  - Cost ceiling: --max-cost STOPS execution when projected total
    exceeds the value (defaults to no ceiling).
  - --dry-run prints what would be updated without patching.
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawlers.llm_parser import extract_lot_fields, HAIKU_MODEL


def _load_env():
    env_path = Path(__file__).resolve().parent.parent / ".env.local"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


_load_env()
URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SB_R = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
SB_W = {**SB_R, "Content-Type": "application/json"}

# Haiku 4.5 pricing per million tokens
PRICE_IN = 0.80
PRICE_OUT = 4.0


def _pg_list(filt: str, limit_total=None):
    """Paginate PostgREST query, return all rows."""
    rows = []
    off = 0
    while True:
        r = requests.get(
            f"{URL}/rest/v1/sale_results?{filt}&offset={off}&limit=1000",
            headers=SB_R, timeout=30,
        )
        chunk = r.json()
        if not isinstance(chunk, list) or not chunk:
            break
        rows.extend(chunk)
        if limit_total and len(rows) >= limit_total:
            rows = rows[:limit_total]; break
        if len(chunk) < 1000:
            break
        off += 1000
    return rows


DIM_PARSE_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:[x×]|by)\s*(\d+(?:[.,]\d+)?)\s*cm",
    re.IGNORECASE,
)


def _parse_dims_text(text: str):
    """Pull (w, h) from a dimensions_text string like '29 x 40 cm'.
    Returns (None, None) when no match.  Both decimal-dot and decimal-
    comma are accepted ('39,5 x 50 cm' → (39.5, 50.0))."""
    if not text:
        return None, None
    m = DIM_PARSE_RE.search(text)
    if not m:
        return None, None
    try:
        a = float(m.group(1).replace(",", "."))
        b = float(m.group(2).replace(",", "."))
    except ValueError:
        return None, None
    if not (1 <= a <= 1000 and 1 <= b <= 1000):
        return None, None
    return a, b


def _build_patch(parsed: dict, current: dict, refresh: bool):
    """Decide which DB columns to update from LLM output.

    The goal is to fill blanks, not overwrite good data, unless
    --refresh is set.
    """
    patch = {}
    # medium — overwrite when current is blank or known-contaminated
    cur_medium = (current.get("medium") or "").strip()
    new_medium = (parsed.get("medium") or "").strip()
    if new_medium and (refresh or not cur_medium
                        or "signed" in cur_medium.lower()
                        or "signé" in cur_medium.lower()
                        or "lower right" in cur_medium.lower()
                        or "lower left" in cur_medium.lower()
                        or "view:" in cur_medium.lower()
                        or len(cur_medium) < 6):
        patch["medium"] = new_medium[:300]
    # year — only fill if blank
    new_year = parsed.get("year")
    if new_year and (refresh or not current.get("year")):
        patch["year"] = new_year
    # provenance — fill if blank
    new_prov = (parsed.get("provenance") or "").strip()
    if new_prov and (refresh or not current.get("provenance")):
        patch["provenance"] = new_prov[:2000]
    # estimate_low / estimate_high / currency — fill blanks.  LLM now
    # extracts these from 'Estimation: X - Y €' and similar blocks.
    el = parsed.get("estimate_low")
    eh = parsed.get("estimate_high")
    ecur = parsed.get("estimate_currency")
    if el and eh and (refresh or current.get("estimate_low") is None):
        patch["estimate_low"] = el
        patch["estimate_high"] = eh
        # Only set currency when none on the row yet
        if ecur and not current.get("currency"):
            patch["currency"] = ecur
    # hammer_price — fill when missing.  When LLM gives one, also
    # recompute price_usd + price_with_premium_usd if possible.
    ham = parsed.get("hammer_price")
    ham_cur = parsed.get("hammer_currency") or ecur
    if ham and (refresh or current.get("hammer_price") is None):
        FX = {"USD":1.0,"EUR":1.08,"GBP":1.27,"HKD":0.128,"CHF":1.13,
              "JPY":0.0067,"CNY":0.137,"SGD":0.74,"MYR":0.22,"AUD":0.66,"THB":0.027}
        patch["hammer_price"] = ham
        cur_for_fx = ham_cur or current.get("currency") or "EUR"
        fx = FX.get(cur_for_fx.upper() if isinstance(cur_for_fx,str) else "EUR", 1.0)
        new_usd = round(ham * fx, 2)
        new_premium = round(new_usd * 1.25, 2)
        patch["price_usd"] = new_usd
        patch["price_with_premium_usd"] = new_premium
        if current.get("area_m2"):
            patch["price_per_m2_usd"] = round(new_premium / current["area_m2"], 2)
        # If status was estimate_only and we now have hammer, mark sold
        if current.get("status") == "estimate_only":
            patch["status"] = "sold"
    # artwork_title — only overwrite if clearly cleaner (current is dim mess)
    cur_title = (current.get("artwork_title") or "").strip()
    new_title = (parsed.get("title") or "").strip()
    if new_title and (refresh or not cur_title
                       or "dimensions" in cur_title.lower()
                       or "x" in cur_title and any(c.isdigit() for c in cur_title)
                       and len(cur_title) > 50):
        if not cur_title or len(new_title) < 200:
            patch["artwork_title"] = new_title[:300]
    # dimensions — overwrite when current values look wrong.  Millon's
    # legacy parser stripped the decimal comma ("48,8" → "488"), so
    # width_cm > 200 with no decimal AND LLM gives a sub-200 value is
    # a strong signal the LLM is right.
    cur_w = current.get("width_cm")
    cur_h = current.get("height_cm")
    dim_text = parsed.get("dimensions_text")
    if dim_text:
        new_w, new_h = _parse_dims_text(dim_text)
        if new_w and new_h:
            # Detect Millon's legacy comma-stripping bug: a value > 200
            # with no decimal part is very likely "39,5" stored as 395.
            # When EITHER current dim shows that pattern AND the LLM
            # gives a coherent sub-200 alternative, overwrite.
            def _looks_comma_stripped(v):
                return v is not None and v > 200 and v == int(v)
            cur_bad = (cur_w is None or cur_h is None
                       or _looks_comma_stripped(cur_w)
                       or _looks_comma_stripped(cur_h))
            # Don't overwrite when LLM result implausibly large
            if new_w > 500 or new_h > 500:
                cur_bad = False
            if refresh or cur_bad:
                # Source-specific orientation: most catalogs write H × W
                # in the text (Bonhams, Sotheby's, Aguttes, Millon,
                # Drouot, Gros-Delettrez, Tajan, Artcurial, Osenat,
                # Invaluable).  Christie's, Phillips, Le Auction write
                # W × H.  Without this convention we can't reliably
                # distinguish portrait vs landscape from the dim text
                # alone.  See artonis_price_mvp._HW_FIRST_SOURCES.
                HW_FIRST = {
                    "bonhams", "sothebys", "aguttes", "drouot",
                    "gros-delettrez", "gros_delettrez", "tajan",
                    "artcurial", "millon", "millon_vn", "osenat",
                    "invaluable",
                }
                src = (current.get("source") or "").lower()
                if src in HW_FIRST:
                    # First number is H, second is W
                    h, w = new_w, new_h
                else:
                    # W × H source (Christie's, Phillips, Le Auction)
                    w, h = new_w, new_h
                patch["width_cm"] = w
                patch["height_cm"] = h
                patch["area_m2"] = round(w * h / 10000, 4)
                # Pretty form for the dimensions string column
                def _fmt(n):
                    return f"{int(n)}" if abs(n - int(n)) < 0.01 else f"{n:.1f}"
                patch["dimensions"] = f"{_fmt(w)} x {_fmt(h)} cm"
                # If price already known, recompute $/m²
                ppm_basis = current.get("price_with_premium_usd") or current.get("price_usd")
                if ppm_basis:
                    patch["price_per_m2_usd"] = round(ppm_basis / patch["area_m2"], 2)
    return patch


def run(source: str = None, limit: int = None, refresh: bool = False,
        delay: float = 0.5, dry_run: bool = False, max_cost: float = None,
        verbose: bool = True):
    flt = ("catalog_description=not.is.null"
           "&select=id,source,artwork_title,medium,year,provenance,catalog_description,"
           "width_cm,height_cm,area_m2,price_usd,price_with_premium_usd,"
           "estimate_low,estimate_high,hammer_price,currency,status")
    if source:
        flt = f"source=eq.{source}&" + flt
    if not refresh:
        # Skip rows that already look clean (medium has no signature
        # markers AND year is set).
        pass  # client-side filter below
    rows = _pg_list(flt, limit_total=limit)
    if verbose:
        print(f"Candidate lots: {len(rows)}")

    total_in = total_out = 0
    updates = 0
    skipped_clean = 0
    errors = 0
    cost_running = 0.0
    for i, lot in enumerate(rows, 1):
        cur_medium = (lot.get("medium") or "")
        cur_year = lot.get("year")
        if (not refresh) and cur_medium and cur_year:
            # Already populated AND medium looks clean (no signature noise)
            ml = cur_medium.lower()
            if not any(m in ml for m in ("signed", "signé", "signe ",
                                          "lower right", "lower left",
                                          "view:", "dimensions")):
                skipped_clean += 1
                continue
        desc = lot.get("catalog_description") or ""
        if len(desc) < 30:
            continue
        result = extract_lot_fields(desc, lot.get("artwork_title") or "")
        if "error" in result:
            errors += 1
            if verbose:
                print(f"  ✗ {lot['id']}: {result['error']}")
            continue
        usage = result.pop("_usage", {})
        in_tok = usage.get("input_tokens") or 0
        out_tok = usage.get("output_tokens") or 0
        total_in += in_tok
        total_out += out_tok
        cost_running = (total_in * PRICE_IN + total_out * PRICE_OUT) / 1_000_000
        if max_cost and cost_running > max_cost:
            if verbose:
                print(f"\n!! Cost ceiling ${max_cost} reached at {i} lots — stopping")
            break

        patch = _build_patch(result, lot, refresh)
        if not patch:
            continue
        if dry_run:
            updates += 1
            if verbose and updates <= 10:
                print(f"  DRY {lot['id']}: {patch}")
        else:
            r = requests.patch(
                f"{URL}/rest/v1/sale_results?id=eq.{lot['id']}",
                headers=SB_W, json=patch, timeout=30,
            )
            if r.status_code in (200, 204):
                updates += 1
                if verbose and updates <= 5:
                    keys = ",".join(patch.keys())
                    print(f"  ✓ {lot['id']}: {keys}")
            else:
                errors += 1
                if verbose:
                    print(f"  ✗ {lot['id']}: HTTP {r.status_code} {r.text[:80]}")
        if verbose and i % 50 == 0:
            print(f"  ... {i}/{len(rows)}  upd={updates}  skip={skipped_clean}  "
                  f"err={errors}  cost=${cost_running:.3f}")
        time.sleep(delay)

    print(f"\nDone.  updated={updates}  skipped_clean={skipped_clean}  errors={errors}")
    print(f"Tokens: {total_in} in + {total_out} out")
    print(f"Cost: ${cost_running:.4f}  (${cost_running/max(1, updates):.5f}/updated lot)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", help="filter by source")
    p.add_argument("--limit", type=int, help="cap lots this run")
    p.add_argument("--refresh", action="store_true",
                   help="re-extract even when current fields look clean")
    p.add_argument("--delay", type=float, default=0.5)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-cost", type=float, help="USD ceiling per run")
    args = p.parse_args()
    run(source=args.source, limit=args.limit, refresh=args.refresh,
        delay=args.delay, dry_run=args.dry_run, max_cost=args.max_cost)
