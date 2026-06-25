"""Pull realized hammer prices from Sothebys + Invaluable via a logged-in
Playwright browser.  Runs ON THE OPERATOR'S MAC — not on GitHub Actions —
because residential IP + real-browser fingerprint together beat both
sites' bot detection.

Cookies live in .env.local as SOTHEBYS_COOKIE / INVALUABLE_COOKIE.  Both
are pasted by the operator from the Network-tab cookie header.

Run:
  # First time — verify it can read 1 lot from each source
  python3 supabase/pull_hammers_local.py --probe

  # Normal pull (max 10 lots per source per run)
  python3 supabase/pull_hammers_local.py

  # Pull only one source
  python3 supabase/pull_hammers_local.py --source sothebys
  python3 supabase/pull_hammers_local.py --source invaluable

  # Tune per-run cap
  python3 supabase/pull_hammers_local.py --limit 20

Rate-limit: 60-120s random sleep between lots.  Conservative on purpose
— better one missed lot today than a banned account next week.
"""
from __future__ import annotations
import os, sys, re, time, random, argparse
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    env_path = ROOT / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


_load_env()
URL = os.environ.get("SUPABASE_URL")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}",
     "Content-Type": "application/json", "Prefer": "return=minimal"}


# ─── Cookie parsing ────────────────────────────────────────────────

def _parse_cookie_string(cookie_str: str, domain: str) -> list[dict]:
    """Convert 'a=1; b=2; c=3' into Playwright-compatible cookie list."""
    out = []
    for piece in cookie_str.split(";"):
        piece = piece.strip()
        if "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        out.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": domain,
            "path": "/",
            "secure": True,
            "httpOnly": False,  # Playwright doesn't care; site does.
            "sameSite": "Lax",
        })
    return out


# ─── DB queries ────────────────────────────────────────────────────

def fetch_missing_hammers(source: str, limit: int) -> list[dict]:
    """Find rows that look like they should have a hammer but don't.

    Priority order:
      1. estimate_only with high estimate_low (worth the call)
      2. recent sale_date (within 12 months)
      3. has area_m2 (we can compute $/m² once hammer lands)
    """
    params = {
        "select": "id,source_url,artist_name_raw,artwork_title,sale_date,estimate_low,estimate_high,currency",
        "source": f"eq.{source}",
        "hammer_price": "is.null",
        "source_url": "not.is.null",
        "order": "estimate_low.desc.nullslast,sale_date.desc.nullslast",
        "limit": str(limit),
    }
    r = requests.get(f"{URL}/rest/v1/sale_results", params=params, headers=H, timeout=20)
    return r.json() if r.ok else []


def patch_hammer(row_id: int, hammer: float, currency: str, fx: dict) -> bool:
    fx_to_usd = fx.get(currency.upper(), 1.0)
    price_usd = round(hammer * fx_to_usd, 2)
    premium = round(hammer * 1.25, 2)  # default 25% buyer premium
    premium_usd = round(premium * fx_to_usd, 2)
    # Get area for $/m²
    rr = requests.get(f"{URL}/rest/v1/sale_results",
                      params={"id": f"eq.{row_id}", "select": "area_m2"},
                      headers=H, timeout=10)
    area = (rr.json()[0].get("area_m2") if rr.ok and rr.json() else None)
    ppm = round(price_usd / area, 2) if area else None
    patch = {
        "hammer_price": hammer,
        "currency": currency.upper(),
        "price_usd": price_usd,
        "price_with_premium": premium,
        "price_with_premium_usd": premium_usd,
        "price_per_m2_usd": ppm,
        "status": "sold",  # DB guard verifies hammer is non-null
    }
    pr = requests.patch(f"{URL}/rest/v1/sale_results",
                       params={"id": f"eq.{row_id}"},
                       headers=H, json=patch, timeout=10)
    return pr.status_code < 300


# ─── Hammer extractors ─────────────────────────────────────────────

# Sothebys: the "Lot Sold: 5,080,000 HKD" text is loaded by AJAX ~3s
# after page load.  Initial HTML has sold:{__typename:"ResultHidden"};
# after AJAX, the rendered DOM (and updated Apollo cache) has the
# real value.  Patterns below match BOTH the JSON cache and the
# visible DOM text.
SOTHEBYS_HAMMER_PATTERNS = [
    # Apollo cache after AJAX update: sold:{__typename:"LotSold",amount:{value:N,currency:"X"}}
    re.compile(r'sold[\\"]*:\s*\{[^}]*?__typename[\\"]*:\s*[\\"]*LotSold[^}]*?value[\\"]*:\s*[\\"]*?([\d.]+)[^}]*?currency[\\"]*:\s*[\\"]*([A-Z]{3})', re.DOTALL),
    # Loose JSON: amount + currency near each other
    re.compile(r'amount[\\"]*:\s*[\\"]*?([\d.]+)[^}]{0,60}currency[\\"]*:\s*[\\"]*([A-Z]{3})', re.DOTALL),
    # Rendered DOM: "Lot Sold" label then number + currency code
    re.compile(r'Lot\s+Sold[\s\S]{0,300}?([\d,]+)\s*(HKD|GBP|USD|EUR|CHF|JPY|CNY|SGD|AUD|CAD)', re.IGNORECASE),
    # Or number + currency directly with currency before
    re.compile(r'(HKD|GBP|USD|EUR|CHF|JPY|CNY|SGD|AUD|CAD)\s*([\d,]+)\s*(?:Lot\s+Sold|sold)', re.IGNORECASE),
]


def _parse_sothebys_hammer(html: str) -> tuple[float | None, str | None]:
    for pat in SOTHEBYS_HAMMER_PATTERNS:
        m = pat.search(html)
        if not m:
            continue
        try:
            groups = m.groups()
            # Figure out which group is number vs currency
            for g in groups:
                if not g:
                    continue
                clean = g.replace(",", "").replace(".", "")
                if clean.isdigit():
                    amt = float(g.replace(",", ""))
                    if amt < 100:
                        continue  # spurious match (page numbers etc)
                    # find currency in the other group
                    cur = next((x for x in groups if x and x not in (g,) and len(x) == 3 and x.isupper()), None)
                    if cur:
                        return amt, cur
        except (ValueError, IndexError):
            continue
    return None, None


def _walk_json_for_hammer(obj, _key_path=""):
    """Walk a JSON-decoded dict/list looking for a hammer-shaped value.

    The bsp-api response is nested; we don't know the exact schema yet,
    so search for any (amount-looking-number + currency-code) pair under
    keys that suggest 'sold' / 'hammer' / 'realised' rather than 'estimate'.
    Returns (amount, currency) or (None, None).
    """
    HIT_KEYS = ("sold", "hammer", "realis", "realiz", "finalprice", "winningbid", "result")
    SKIP_KEYS = ("estimate", "low", "high", "reserve", "opening")
    CURRENCIES = {"USD","EUR","GBP","HKD","CHF","JPY","CNY","SGD","AUD","CAD"}

    def _looks_like_amount_obj(d):
        if not isinstance(d, dict):
            return None
        amt = d.get("value") or d.get("amount") or d.get("price")
        cur = d.get("currency") or d.get("currencyCode") or d.get("isoCode")
        if amt is None or cur is None:
            return None
        try:
            a = float(amt)
        except (TypeError, ValueError):
            return None
        if isinstance(cur, str) and cur.upper() in CURRENCIES and a >= 100:
            return a, cur.upper()
        return None

    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = (k or "").lower()
            in_path = _key_path + "/" + kl
            if any(s in kl for s in SKIP_KEYS):
                continue
            if any(h in kl for h in HIT_KEYS):
                hit = _looks_like_amount_obj(v)
                if hit:
                    return hit
            r = _walk_json_for_hammer(v, in_path)
            if r[0] is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _walk_json_for_hammer(item, _key_path)
            if r[0] is not None:
                return r
    return None, None


# Discovered 2026-06-25 via operator's DevTools inspection:
# Sothebys's hammer lives in a GraphQL response from
# https://clientapi.prod.sothelabs.com/graphql (separate subdomain!).
# Auth is a Bearer JWT, not the cookie itself.  Cookies log us into
# sothebys.com → Auth0 SPA SDK exchanges cookie for an access_token
# stored in localStorage → that token is the Bearer.
#
# The hammer is at: data.lot.bidState.sold.premiums[].finalPriceV2.amount
# Currency at:      data.lot.auction.currencyV2 (or auction.currency)
SOTHEBYS_GRAPHQL_URL = "https://clientapi.prod.sothelabs.com/graphql"

# Minimal slice of the operator-captured LotQuery — only the fields
# we need.  Sothebys backend accepts custom queries (no persisted-
# query gate), so we send a stripped version.
_SOTHEBYS_QUERY = (
    "query LotHammer($id: String!, $countryOfOrigin: String, "
    "$language: TranslationLanguage!) {\n"
    "  lot: lotV2(lotId: $id, countryOfOrigin: $countryOfOrigin, language: $language) {\n"
    "    __typename\n"
    "    ... on LotV2 {\n"
    "      lotId\n"
    "      title\n"
    "      auction { currency currencyV2 }\n"
    "      bidState {\n"
    "        isClosed\n"
    "        sold {\n"
    "          __typename\n"
    "          ... on ResultVisible {\n"
    "            isSold\n"
    "            premiums {\n"
    "              finalPriceV2 { amount }\n"
    "            }\n"
    "          }\n"
    "        }\n"
    "        currentBidV2 { amount }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}"
)


def _extract_sothebys_bearer(page) -> str | None:
    """Pull the Auth0 access_token from localStorage.  Sothebys uses
    @@auth0spajs@@ keys (the standard Auth0 SPA SDK pattern)."""
    js = """
    () => {
      try {
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          if (!k) continue;
          if (k.includes('auth0') || k.includes('access_token') || k.includes('sothebys')) {
            try {
              const v = JSON.parse(localStorage.getItem(k));
              if (v && v.body && v.body.access_token) return v.body.access_token;
              if (v && v.access_token) return v.access_token;
              if (v && v.body && v.body.id_token) return v.body.id_token;
            } catch(e) {}
          }
        }
      } catch(e) {}
      return null;
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return None


def _extract_sothebys_auction_id(page) -> str | None:
    html = page.content()
    m = re.search(r'"auctionId"\s*:\s*"([0-9a-fA-F-]{36})"', html)
    return m.group(1) if m else None


def _fetch_sothebys_hammer_via_api(page, lot_url, probe=False, source_label="sothebys"):
    """Cookie → Bearer → GraphQL → hammer.

    Two paths run in parallel:
      A. Auth0 SDK runs in headed Chrome, deposits token in localStorage.
         We extract it and make our own minimal GraphQL call.
      B. Sothebys's own JS fires LotQuery to clientapi.sothelabs.com.
         We listen for the response and parse it directly — no Bearer
         extraction needed.  Path B is cheaper but only works if their
         JS executes the call.
    """
    import json as _json

    # Path B: collect ALL graphql responses (Sothebys fires multiple
    # operations: LotQuery, UserBidQuery, etc) and any Bearer carried
    # in the Authorization header of an outgoing request.
    captured = {"responses": [], "bearer": None}

    def on_request(req):
        if captured["bearer"] is not None:
            return
        if "clientapi.prod.sothelabs.com" in req.url:
            auth = req.headers.get("authorization") or req.headers.get("Authorization")
            if auth and auth.lower().startswith("bearer "):
                captured["bearer"] = auth[7:]

    def on_response(resp):
        if "clientapi.prod.sothelabs.com/graphql" not in resp.url:
            return
        try:
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype.lower():
                return
            txt = resp.text()
            data = _json.loads(txt)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        # Tag with the operation name from the request if possible
        op = None
        try:
            req = resp.request
            if req:
                post = req.post_data or ""
                m_op = re.search(r'"operationName"\s*:\s*"([^"]+)"', post)
                if m_op:
                    op = m_op.group(1)
        except Exception:
            pass
        captured["responses"].append({"op": op, "data": data, "url": resp.url})

    page.on("request", on_request)
    page.on("response", on_response)

    try:
        page.goto(lot_url, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(10_000)
    except Exception as e:
        print(f"      ✗ goto failed: {type(e).__name__}: {str(e)[:80]}")
        try: page.remove_listener("request", on_request)
        except Exception: pass
        try: page.remove_listener("response", on_response)
        except Exception: pass
        return None, None

    try: page.remove_listener("request", on_request)
    except Exception: pass
    try: page.remove_listener("response", on_response)
    except Exception: pass

    if probe:
        print(f"      captured {len(captured['responses'])} graphql response(s)")
        for r in captured["responses"]:
            print(f"        - op={r['op']}")
        print(f"      bearer captured: {'yes' if captured['bearer'] else 'no'}")

    # Try parsing every captured response for hammer — first match wins
    for r in captured["responses"]:
        amt, cur = _parse_sothebys_graphql_response(r["data"])
        if amt is not None:
            if probe:
                sample = ROOT / f"sample_{source_label}_graphql_{r['op'] or 'unknown'}.json"
                sample.write_text(_json.dumps(r["data"], indent=2, ensure_ascii=False))
                print(f"      ✓ hammer found in op={r['op']}, saved to {sample.name}")
            return amt, cur

    # No captured response had a hammer.  If we DO have a bearer, fire
    # our own minimal LotHammer query as a last resort.
    if probe and captured["responses"]:
        # Dump them all so we can inspect shapes
        for i, r in enumerate(captured["responses"], 1):
            sample = ROOT / f"sample_{source_label}_graphql_{i}_{r['op'] or 'unknown'}.json"
            sample.write_text(_json.dumps(r["data"], indent=2, ensure_ascii=False))
        print(f"      ◆ no hammer parsed; all {len(captured['responses'])} responses dumped for inspection")

    bearer = captured["bearer"]

    # Path A: extract Bearer + call GraphQL ourselves
    # Bearer: prefer the one captured from outgoing requests (real
    # token, runtime-issued).  Only fall back to localStorage scan if
    # we didn't observe one — but Sothebys uses Auth0 memory mode so
    # localStorage will normally be empty.
    if not bearer:
        bearer = _extract_sothebys_bearer(page)
    if not bearer:
        print("      ✗ no Bearer captured AND no graphql response had a hammer")
        print("        Sothebys's auth0 SDK appears to have skipped under Playwright.")
        print("        Try opening the lot once in normal Chrome first to refresh cookies.")
        return None, None
    if probe:
        print(f"      bearer: {bearer[:40]}…{bearer[-20:]}")

    html = page.content()
    m = re.search(r'"lotId"\s*:\s*"([0-9a-fA-F-]{36})"', html)
    if not m:
        print(f"      ✗ couldn't extract lotId from page ({len(html)} chars)")
        return None, None
    lot_id = m.group(1)
    auction_id = _extract_sothebys_auction_id(page)
    if probe:
        print(f"      lotId: {lot_id}")
        print(f"      auctionId: {auction_id}")

    # POST to the GraphQL endpoint from inside the page so origin
    # / referer match what Sothebys's CORS expects.
    js = (
        "async (args) => {\n"
        "  const { bearer, query, variables, url } = args;\n"
        "  const r = await fetch(url, {\n"
        "    method: 'POST',\n"
        "    headers: {\n"
        "      'Content-Type': 'application/json',\n"
        "      'Accept': '*/*',\n"
        "      'Authorization': 'Bearer ' + bearer,\n"
        "      'apollographql-client-name': 'Bidclient',\n"
        "    },\n"
        "    body: JSON.stringify({\n"
        "      operationName: 'LotHammer',\n"
        "      query, variables,\n"
        "    }),\n"
        "  });\n"
        "  return { status: r.status, contentType: r.headers.get('content-type'), text: await r.text() };\n"
        "}"
    )
    args = {
        "bearer": bearer,
        "query": _SOTHEBYS_QUERY,
        "variables": {
            "id": lot_id,
            "countryOfOrigin": "IE",
            "language": "ENGLISH",
        },
        "url": SOTHEBYS_GRAPHQL_URL,
    }
    try:
        result = page.evaluate(js, args)
    except Exception as e:
        print(f"      ✗ GraphQL fetch failed: {type(e).__name__}: {str(e)[:120]}")
        return None, None

    status = result.get("status")
    txt = result.get("text") or ""
    if probe:
        print(f"      graphql: status={status}, body {len(txt)} chars")
    if status != 200:
        print(f"      ✗ graphql non-200: {status}, body[0:300]={txt[:300]!r}")
        return None, None
    try:
        data = _json.loads(txt)
    except Exception as e:
        print(f"      ✗ graphql JSON parse failed: {e}")
        print(f"        body[0:200]={txt[:200]!r}")
        return None, None

    if probe:
        sample = ROOT / f"sample_{source_label}_graphql.json"
        sample.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
        print(f"      ◆ graphql JSON written to {sample}")

    return _parse_sothebys_graphql_response(data)


def _parse_sothebys_graphql_response(data: dict) -> tuple[float | None, str | None]:
    """Pull hammer + currency from a Sothebys LotQuery/LotHammer GraphQL
    response.  Tolerates both shapes (Sothebys's full query vs our
    minimal one) by searching common paths and falling back to
    walker.
    """
    try:
        lot = (data.get("data") or {}).get("lot") or {}
        bid_state = lot.get("bidState") or {}
        sold = bid_state.get("sold") or {}
        tname = sold.get("__typename")
        currency = ((lot.get("auction") or {}).get("currencyV2")
                    or (lot.get("auction") or {}).get("currency"))
        if tname == "ResultVisible":
            prems = sold.get("premiums") or []
            for p in prems:
                fp = p.get("finalPriceV2") or p.get("finalPrice") or {}
                amt = fp.get("amount")
                if amt:
                    return float(amt), (currency or "USD").upper()
        # Fallback paths sometimes seen in the operator-captured payload
        for key in ("finalPrice", "currentBid"):
            v = bid_state.get(key)
            if isinstance(v, dict):
                amt = v.get("amount")
                if amt:
                    return float(amt), (currency or "USD").upper()
    except (TypeError, ValueError) as e:
        pass
    # Final fallback: structure-agnostic walker
    return _walk_json_for_hammer(data)


# Invaluable: when logged in, the lot data island has the real sold
# amount.  Discovered 2026-06-26 by probe: the relevant fields are
# embedded in the page's __NEXT_DATA__ / preloaded state.
#   "isLotClosed":true,"lotRef":"F474...","currentBid":80000,"soldAmount":80000
# So we look for soldAmount (post-sale truth), with currentBid as a
# fallback for lots where soldAmount isn't present yet.
INVALUABLE_HAMMER_PATTERNS = [
    re.compile(r'"soldAmount"\s*:\s*([\d.]+)'),
    re.compile(r'"isLotClosed"\s*:\s*true[^}]*"currentBid"\s*:\s*([\d.]+)'),
    re.compile(r'"realizedPrice"\s*:\s*([\d.]+)'),
    re.compile(r'"hammerPrice"\s*:\s*([\d.]+)'),
]

# Currency for Invaluable lives in a separate field — look it up near
# soldAmount.  Falls back to USD when missing (most lots).
INVALUABLE_CURRENCY_PATTERNS = [
    re.compile(r'"currency"\s*:\s*"([A-Z]{3})"'),
    re.compile(r'"currencyCode"\s*:\s*"([A-Z]{3})"'),
]


def _parse_invaluable_hammer(html: str) -> tuple[float | None, str | None]:
    # Find sold amount first
    amt = None
    for pat in INVALUABLE_HAMMER_PATTERNS:
        m = pat.search(html)
        if m:
            try:
                amt = float(m.group(1))
                if amt > 0:
                    break
                amt = None
            except (ValueError, IndexError):
                continue
    if amt is None:
        return None, None
    # Currency
    for pat in INVALUABLE_CURRENCY_PATTERNS:
        m = pat.search(html)
        if m and m.group(1) in {"USD","EUR","GBP","HKD","CAD","AUD","SGD","CHF","JPY","CNY","MYR","THB"}:
            return amt, m.group(1)
    return amt, "USD"  # safe default


# ─── Main ──────────────────────────────────────────────────────────

FX = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.26, "CAD": 0.74, "AUD": 0.66,
    "HKD": 0.128, "SGD": 0.74, "CHF": 1.10, "JPY": 0.0064, "CNY": 0.14,
}


def process_source(source: str, cookie: str, domain: str,
                   parse_fn, limit: int, probe: bool) -> None:
    if not cookie:
        print(f"  [{source}] no cookie in env — skip")
        return

    rows = fetch_missing_hammers(source, limit=1 if probe else limit)
    if not rows:
        print(f"  [{source}] no lots missing hammer — skip")
        return
    print(f"  [{source}] {len(rows)} lots queued")

    from playwright.sync_api import sync_playwright
    # Sothebys's Auth0 SDK skips silently under headless Chrome (likely
    # a navigator.webdriver guard).  Without the SDK, no Bearer token is
    # issued and the GraphQL call never fires.  Run headed for Sothebys.
    # A Chrome window will briefly appear; close it gets handled by the
    # context manager exit.
    headless = (source != "sothebys")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        context.add_cookies(_parse_cookie_string(cookie, domain))
        page = context.new_page()

        n_ok = n_fail = 0
        for i, row in enumerate(rows, 1):
            url = row["source_url"]
            print(f"\n  [{i}/{len(rows)}] {row['artist_name_raw']} | {(row.get('artwork_title') or '')[:50]}")
            print(f"      URL: {url[-80:]}")
            # Sothebys: capture the bsp-api/lot/details response directly
            # (the AJAX the operator described — page initially shows
            # 'Log in to view', then the API responds and the hammer
            # replaces the button).  Cookies authenticate the call.
            if source == "sothebys":
                amt, cur = _fetch_sothebys_hammer_via_api(page, url, probe=probe, source_label=source)
            else:
                try:
                    page.goto(url, timeout=30_000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)
                    html = page.content()
                except Exception as e:
                    print(f"      ✗ fetch failed: {type(e).__name__}: {str(e)[:80]}")
                    n_fail += 1
                    continue
                amt, cur = parse_fn(html)

            if amt is None:
                if source != "sothebys":
                    print(f"      ✗ no hammer parsed from page")
                n_fail += 1
                if probe and source != "sothebys":
                    # Save HTML so we can inspect what real hammer looks like
                    sample = ROOT / f"sample_{source}_logged_in.html"
                    sample.write_text(page.content())
                    print(f"      ◆ HTML sample written to {sample}")
            else:
                print(f"      ✓ hammer = {cur} {amt:,.0f}")
                if patch_hammer(row["id"], amt, cur, FX):
                    n_ok += 1
                else:
                    print("      ✗ DB patch failed")
                    n_fail += 1

            # Conservative random sleep — 60-120s between lots
            if i < len(rows):
                sleep_s = random.uniform(60, 120) if not probe else 5
                print(f"      ... sleep {sleep_s:.0f}s")
                time.sleep(sleep_s)

        browser.close()
    print(f"\n  [{source}] done: {n_ok} OK, {n_fail} failed")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["sothebys", "invaluable", "both"], default="both")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--probe", action="store_true",
                    help="Process just 1 lot per source, dump HTML for inspection")
    args = ap.parse_args()

    print("=" * 70)
    print("Local hammer puller (Mac, residential IP, real Chrome)")
    print("=" * 70)

    if args.source in ("sothebys", "both"):
        process_source(
            "sothebys",
            os.environ.get("SOTHEBYS_COOKIE", ""),
            ".sothebys.com",
            _parse_sothebys_hammer,
            args.limit,
            args.probe,
        )

    if args.source in ("invaluable", "both"):
        process_source(
            "invaluable",
            os.environ.get("INVALUABLE_COOKIE", ""),
            ".invaluable.com",
            _parse_invaluable_hammer,
            args.limit,
            args.probe,
        )

    print("\nDone.")
    if not args.probe:
        print("Refreshing artist stats…")
        import subprocess
        subprocess.run(["python3", str(ROOT / "supabase" / "refresh_artist_stats.py")], check=False)


if __name__ == "__main__":
    main()
