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
        # Exclude lots we already know didn't sell — saves rate-limit
        # budget for lots that might have a real hammer.  Only enum
        # values actually in sale_status; 'unsold' isn't one of them
        # so don't list it (PostgREST 400s on unknown enum members).
        "status": "not.in.(passed,withdrawn)",
        "order": "estimate_low.desc.nullslast,sale_date.desc.nullslast",
        "limit": str(limit),
    }
    r = requests.get(f"{URL}/rest/v1/sale_results", params=params, headers=H, timeout=20)
    return r.json() if r.ok else []


def _mark_invaluable_unsold(row_id: int) -> bool:
    """Mark a lot 'passed' so the puller stops re-checking it next run.
    'passed' is already in the schema (DB count: 1 row pre-existing)."""
    r = requests.patch(f"{URL}/rest/v1/sale_results",
                       params={"id": f"eq.{row_id}"},
                       headers=H, json={"status": "passed"}, timeout=10)
    return r.status_code < 300


def patch_image_only(row_id: int, image_url: str) -> bool:
    """Set image_url on a row that already has hammer / is unsold.
    Skipped when the row already has an image_url."""
    rr = requests.get(f"{URL}/rest/v1/sale_results",
                      params={"id": f"eq.{row_id}", "select": "image_url"},
                      headers=H, timeout=10)
    if rr.ok and rr.json() and rr.json()[0].get("image_url"):
        return False
    pr = requests.patch(f"{URL}/rest/v1/sale_results",
                       params={"id": f"eq.{row_id}"},
                       headers={**H, "Prefer": "return=minimal"},
                       json={"image_url": image_url}, timeout=10)
    return pr.status_code < 300


def _premium_pct_for(source: str) -> float:
    """Buyer's premium rate (%) for a given source.  Looks up the
    registry in data/auction_houses.py; falls back to 25% only when
    the source isn't catalogued.  Centralised so every puller +
    importer derives premium the same way."""
    from data.auction_houses import AUCTION_HOUSES
    return (AUCTION_HOUSES.get(source) or {}).get("premium_rate_pct", 25.0)


def patch_hammer(row_id: int, hammer: float, currency: str, fx: dict,
                 image_url: str | None = None, source: str = "") -> bool:
    fx_to_usd = fx.get(currency.upper(), 1.0)
    price_usd = round(hammer * fx_to_usd, 2)
    rate_pct = _premium_pct_for(source)
    premium = round(hammer * (1 + rate_pct / 100), 2)
    premium_usd = round(premium * fx_to_usd, 2)
    # Get area + existing image for $/m² + don't overwrite image
    rr = requests.get(f"{URL}/rest/v1/sale_results",
                      params={"id": f"eq.{row_id}", "select": "area_m2,image_url"},
                      headers=H, timeout=10)
    row = rr.json()[0] if rr.ok and rr.json() else {}
    area = row.get("area_m2")
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
    # Only set image_url when (a) we have a new one and (b) it's missing
    if image_url and not row.get("image_url"):
        patch["image_url"] = image_url
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
    "      media(imageSizes: [Large, ExtraLarge]) {\n"
    "        images {\n"
    "          renditions { url imageSize width height }\n"
    "        }\n"
    "      }\n"
    "    }\n"
    "  }\n"
    "}"
)


def _extract_sothebys_image(data: dict) -> str | None:
    """Pick the largest rendition URL from Sothebys GraphQL response."""
    try:
        media = (data.get("data", {}).get("lot", {}) or {}).get("media")
        if not media:
            return None
        images = media.get("images") or []
        if not images:
            return None
        # Take first image's largest rendition (sorted by width desc)
        rends = images[0].get("renditions") or []
        if not rends:
            return None
        # Prefer ExtraLarge, else Large
        for size in ("ExtraLarge", "Large"):
            for r in rends:
                if r.get("imageSize") == size and r.get("url"):
                    return r["url"]
        # Fallback: any url
        for r in rends:
            if r.get("url"):
                return r["url"]
    except (TypeError, AttributeError):
        pass
    return None


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


def _sothebys_direct_request(lot_url, bearer, probe=False, source_label="sothebys"):
    """Skip Playwright entirely: GET the lot page (no auth needed for
    HTML) → extract lotId → POST the GraphQL with Bearer.  Much
    faster than driving Chrome, and Sothebys's anti-automation
    only kicks in when there's no Bearer.  Confirmed 2026-06-25
    against the Le Pho HK known lot."""
    import json as _json
    # 1. Get the lot page for lotId
    try:
        r = requests.get(lot_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }, timeout=20)
    except Exception as e:
        print(f"      ✗ lot page fetch failed: {e}")
        return None, None
    if r.status_code != 200:
        print(f"      ✗ lot page non-200: {r.status_code}")
        return None, None
    m = re.search(r'"lotId"\s*:\s*"([0-9a-fA-F-]{36})"', r.text)
    if not m:
        print(f"      ✗ couldn't extract lotId from page ({len(r.text)} chars)")
        return None, None
    lot_id = m.group(1)
    if probe:
        print(f"      lotId: {lot_id}")

    # 2. POST GraphQL with Bearer
    try:
        gr = requests.post(
            SOTHEBYS_GRAPHQL_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Authorization": f"Bearer {bearer}",
                "apollographql-client-name": "Bidclient",
                "origin": "https://www.sothebys.com",
                "referer": "https://www.sothebys.com/",
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            },
            json={
                "operationName": "LotHammer",
                "query": _SOTHEBYS_QUERY,
                "variables": {"id": lot_id, "countryOfOrigin": "IE", "language": "ENGLISH"},
            },
            timeout=20,
        )
    except Exception as e:
        print(f"      ✗ graphql request failed: {e}")
        return None, None
    if gr.status_code != 200:
        print(f"      ✗ graphql non-200: {gr.status_code}, body[0:300]={gr.text[:300]!r}")
        # Common case: 401 = Bearer expired
        if gr.status_code == 401:
            print("        Bearer expired — paste a fresh one to .env.local:SOTHEBYS_BEARER=…")
        return None, None
    try:
        data = gr.json()
    except Exception as e:
        print(f"      ✗ graphql JSON parse: {e}")
        return None, None

    if probe:
        sample = ROOT / f"sample_{source_label}_graphql_LotHammer.json"
        sample.write_text(_json.dumps(data, indent=2, ensure_ascii=False))
        print(f"      ◆ graphql JSON written to {sample}")

    amt, cur = _parse_sothebys_graphql_response(data)
    # Image comes free with the same call — stash it whether or not the
    # lot has a hammer.  Operator 2026-06-27: unsold lots
    # (bidState.sold.isSold=false, no finalPriceV2) still ship a media
    # block with renditions; previously the image was being dropped
    # because we only stashed it when amt != None, so passed/unsold
    # Sothebys rows never got their photo.
    _SOTHEBYS_LAST_IMAGE[0] = _extract_sothebys_image(data)
    return amt, cur


# Tiny module-level cache so the puller loop can read the image URL
# after a hammer call without rewriting the whole signature.  Reset
# per lot.
_SOTHEBYS_LAST_IMAGE = [None]


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
    # Bearer fallback chain:
    #   1. From outgoing request (best — current SDK-issued token)
    #   2. From localStorage (rare — Sothebys uses Auth0 memory mode)
    #   3. From SOTHEBYS_BEARER env var (manual paste from DevTools)
    if not bearer:
        bearer = _extract_sothebys_bearer(page)
    if not bearer:
        env_bearer = os.environ.get("SOTHEBYS_BEARER", "").strip()
        if env_bearer:
            bearer = env_bearer
            if probe:
                print(f"      using SOTHEBYS_BEARER from env: {bearer[:40]}…{bearer[-20:]}")
    if not bearer:
        print("      ✗ no Bearer available (request/storage/env all empty)")
        print("        Workaround: paste a fresh Bearer to .env.local:")
        print("          SOTHEBYS_BEARER=<copy from DevTools graphql request, 'authorization' header>")
        print("        It expires ~24h — refresh when the next run fails.")
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
            prems = sold.get("premiums")
            # premiums is normally a single object {finalPriceV2: {amount}}
            # but tolerate list shape too in case Sothebys changes schema
            if isinstance(prems, dict):
                prems = [prems]
            for p in (prems or []):
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


_INVALUABLE_PASSED_RE = re.compile(r'\b(?:Passed|Withdrawn|Unsold|Reserve\s+Not\s+Met|Not\s+Sold)\b')
_INVALUABLE_CURRENT_BID_RE = re.compile(r'"currentBid"\s*:\s*([\d.]+|null)')
_INVALUABLE_CF_RE = re.compile(r'Just\s+a\s+moment|Checking\s+if\s+the\s+site\s+connection', re.IGNORECASE)


def _parse_invaluable_hammer(html: str) -> tuple[float | None, str | None, str]:
    """Returns (amount, currency, status).
    status ∈ {sold, passed, cf_challenge, unknown}.
    Caller decides what to write — only 'sold' updates hammer_price.
    """
    # Detect Cloudflare challenge page first — short body + 'Just a moment'
    if _INVALUABLE_CF_RE.search(html) or len(html) < 50_000:
        return None, None, "cf_challenge"

    # Detect Passed/Withdrawn lots — soldAmount might be the reserve,
    # not the realized price, so we'd write a fake hammer otherwise.
    if _INVALUABLE_PASSED_RE.search(html):
        return None, None, "passed"

    # Real-sold guard: soldAmount and currentBid must match (when both
    # are present).  For sold lots they're equal; for passed lots
    # currentBid < soldAmount (reserve).
    sold_match = INVALUABLE_HAMMER_PATTERNS[0].search(html)  # soldAmount
    if sold_match:
        try:
            sold_amt = float(sold_match.group(1))
        except (ValueError, TypeError):
            sold_amt = None
        cb_match = _INVALUABLE_CURRENT_BID_RE.search(html)
        if cb_match and cb_match.group(1) != "null":
            try:
                cb = float(cb_match.group(1))
                if sold_amt and abs(cb - sold_amt) / sold_amt > 0.05:
                    # 5%+ mismatch — likely Passed without explicit label
                    return None, None, "passed"
            except (ValueError, TypeError):
                pass

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
        return None, None, "unknown"
    # Currency
    for pat in INVALUABLE_CURRENCY_PATTERNS:
        m = pat.search(html)
        if m and m.group(1) in {"USD","EUR","GBP","HKD","CAD","AUD","SGD","CHF","JPY","CNY","MYR","THB"}:
            return amt, m.group(1), "sold"
    return amt, "USD", "sold"


# ─── Main ──────────────────────────────────────────────────────────

FX = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.26, "CAD": 0.74, "AUD": 0.66,
    "HKD": 0.128, "SGD": 0.74, "CHF": 1.10, "JPY": 0.0064, "CNY": 0.14,
}


def _process_sothebys_direct(rows: list[dict], probe: bool) -> None:
    """Pure-requests flow: no Playwright, no Chrome window.
    Each lot: GET lot_url → extract lotId → POST GraphQL with Bearer.
    """
    bearer = os.environ["SOTHEBYS_BEARER"].strip()
    n_ok = n_fail = 0
    for i, row in enumerate(rows, 1):
        url = row["source_url"]
        print(f"\n  [{i}/{len(rows)}] {row['artist_name_raw']} | {(row.get('artwork_title') or '')[:50]}")
        print(f"      URL: {url[-80:]}")
        _SOTHEBYS_LAST_IMAGE[0] = None  # reset per-lot stash
        amt, cur = _sothebys_direct_request(url, bearer, probe=probe, source_label="sothebys")
        img = _SOTHEBYS_LAST_IMAGE[0]
        if amt is None:
            # No hammer (lot unsold / passed / not yet closed) — but the
            # GraphQL response still carries the lot photo.  Patch the
            # image alone so the row at least gets its thumbnail.
            if img and patch_image_only(row["id"], img):
                print(f"      ⚠ no hammer (unsold) — image saved")
            else:
                print(f"      ✗ no hammer, no image")
            n_fail += 1
        else:
            img_note = f" + image" if img else ""
            print(f"      ✓ hammer = {cur} {amt:,.0f}{img_note}")
            if patch_hammer(row["id"], amt, cur, FX, image_url=img, source="sothebys"):
                n_ok += 1
            else:
                print("      ✗ DB patch failed")
                n_fail += 1
        # Lighter rate-limit for direct path — no browser startup, just
        # 2 small HTTP calls.  Still polite.
        if i < len(rows):
            sleep_s = random.uniform(8, 15) if not probe else 1
            print(f"      ... sleep {sleep_s:.0f}s")
            time.sleep(sleep_s)
    print(f"\n  [sothebys] done: {n_ok} OK, {n_fail} failed")


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

    # Sothebys with SOTHEBYS_BEARER → fully direct (no Playwright).
    # Skips the Chrome window entirely.  Falls back to Playwright only
    # if Bearer is absent.
    if source == "sothebys" and os.environ.get("SOTHEBYS_BEARER", "").strip():
        _process_sothebys_direct(rows, probe=probe)
        return

    from playwright.sync_api import sync_playwright
    # Both Sothebys and Invaluable need headed Chrome:
    #   Sothebys: Auth0 SDK refuses to issue token under headless
    #   Invaluable: Cloudflare returns 'Just a moment' challenge to
    #               headless requests; headed Chrome passes the JS
    #               challenge automatically in 5-8s
    # Operator 2026-06-26: 10/10 Invaluable lots CF-skipped under
    # headless even with stealth init script; flipping to headed.
    headless = source not in ("sothebys", "invaluable")
    with sync_playwright() as p:
        # For Invaluable: use a PERSISTENT Chrome profile so that the
        # cf_clearance cookie obtained from one solve carries to the
        # next run.  channel='chrome' uses the operator's real Chrome.
        # First run shows CF challenge → operator solves once →
        # subsequent runs reuse the same profile → no challenge.
        browser = None
        if source == "invaluable":
            profile_dir = ROOT / ".chrome_profile_invaluable"
            profile_dir.mkdir(exist_ok=True)
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    channel="chrome",
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--disable-infobars",
                    ],
                    # Crucially, drop --enable-automation (Playwright's
                    # default) — that flag is what shows the 'Chrome is
                    # being controlled by automated test software' banner
                    # AND sets navigator.webdriver=true that CF reads.
                    ignore_default_args=["--enable-automation"],
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                    user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/131.0.0.0 Safari/537.36"),
                )
            except Exception as e:
                print(f"      ⚠ persistent Chrome failed: {e}")
                browser = p.chromium.launch(headless=False)
                context = browser.new_context()
            # Also seed our session cookie into the persistent profile
            context.add_cookies(_parse_cookie_string(cookie, domain))
        else:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"),
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            context.add_cookies(_parse_cookie_string(cookie, domain))
        # Stealth applied for BOTH sources — Invaluable's Cloudflare
        # also keys on automation markers.  Operator 2026-06-26: 10/10
        # Invaluable lots returned CF challenge until stealth+wait
        # were added.
        if source in ("sothebys", "invaluable"):
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [{ name: 'PDF Viewer' }, { name: 'Chrome PDF Viewer' }]
                });
                window.chrome = window.chrome || { runtime: {} };
                const originalQuery = window.navigator.permissions ? window.navigator.permissions.query : null;
                if (originalQuery) {
                    window.navigator.permissions.query = (p) =>
                        p && p.name === 'notifications'
                            ? Promise.resolve({ state: Notification.permission })
                            : originalQuery(p);
                }
            """)
        page = context.new_page()

        # Invaluable warmup: first navigate to a generic page so the
        # operator can solve any CF challenge ONCE (subsequent lots
        # reuse the cf_clearance cookie from the persistent profile).
        if source == "invaluable":
            try:
                print("\n  [invaluable] warmup — opening invaluable.com to clear CF")
                page.goto("https://www.invaluable.com/", timeout=60_000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                title = (page.title() or "").lower()
                if "just a moment" in title or "security verification" in title:
                    print("    ⚠ Cloudflare challenge visible — please click the checkbox")
                    print("    (or wait; will auto-continue when page clears)")
                    try:
                        page.wait_for_function(
                            "() => !((document.title||'').toLowerCase().includes('just a moment') "
                            "      || (document.title||'').toLowerCase().includes('security verification'))",
                            timeout=120_000,  # 2 min for operator to solve
                        )
                        print("    ✓ CF cleared, proceeding")
                    except Exception:
                        print("    ⚠ still on challenge page after 2 min — continuing anyway")
                else:
                    print(f"    ✓ no CF challenge (title: {page.title()[:60]!r})")
            except Exception as e:
                print(f"    ⚠ warmup error: {e}")

        n_ok = n_fail = 0
        for i, row in enumerate(rows, 1):
            url = row["source_url"]
            print(f"\n  [{i}/{len(rows)}] {row['artist_name_raw']} | {(row.get('artwork_title') or '')[:50]}")
            print(f"      URL: {url[-80:]}")
            # Sothebys: prefer the direct path (no Playwright) when
            # SOTHEBYS_BEARER is in env — confirmed working 2026-06-25
            # against Le Pho HK lot.  Playwright path stays as fallback
            # for future runs where we discover how to make Auth0 SDK
            # work under automation.
            if source == "sothebys":
                env_bearer = os.environ.get("SOTHEBYS_BEARER", "").strip()
                if env_bearer:
                    amt, cur = _sothebys_direct_request(url, env_bearer, probe=probe, source_label=source)
                else:
                    amt, cur = _fetch_sothebys_hammer_via_api(page, url, probe=probe, source_label=source)
            else:
                try:
                    page.goto(url, timeout=45_000, wait_until="domcontentloaded")
                    # Cloudflare challenge: the first response can be the
                    # 'Just a moment' page; wait for the real lot HTML to
                    # render (Sold/Sold at Auction text, or soldAmount in
                    # data island).  Up to 20s — CF JS challenges resolve
                    # in 5-8s typically.
                    if source == "invaluable":
                        try:
                            # Up to 35s — CF visible challenge ("Performing
                            # security verification") can take longer than
                            # the silent 'Just a moment' variant.
                            page.wait_for_function(
                                "() => {"
                                "  const t = (document.title || '').toLowerCase();"
                                "  if (t.includes('just a moment') || t.includes('security verification')) return false;"
                                "  const html = document.documentElement.innerHTML;"
                                "  return html.length > 60000 && "
                                "    (html.includes('soldAmount') || html.includes('Sold at Auction')"
                                "     || html.includes('isLotClosed'));"
                                "}",
                                timeout=35_000,
                            )
                        except Exception:
                            pass  # parser will detect cf_challenge below
                    else:
                        page.wait_for_timeout(2500)
                    html = page.content()
                except Exception as e:
                    print(f"      ✗ fetch failed: {type(e).__name__}: {str(e)[:80]}")
                    n_fail += 1
                    continue
                # Invaluable parser returns 3-tuple with explicit status
                if source == "invaluable":
                    amt, cur, lot_status = _parse_invaluable_hammer(html)
                    if lot_status == "passed":
                        print("      ⓘ lot Passed (didn't meet reserve) — marking unsold")
                        # Mark as unsold so we stop re-checking it next run
                        _mark_invaluable_unsold(row["id"])
                        n_fail += 1
                        continue
                    if lot_status == "cf_challenge":
                        print("      ⚠ Cloudflare challenge — skipping, retry next run")
                        n_fail += 1
                        continue
                else:
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
                if patch_hammer(row["id"], amt, cur, FX, source=row.get("source", "")):
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
    # Show remaining queue per source so the operator knows how many
    # more Pull_*.command runs are needed.
    if not args.probe and args.source != "both":
        params = {
            "select": "id", "source": f"eq.{args.source}",
            "hammer_price": "is.null", "source_url": "not.is.null",
            "status": "not.in.(passed,withdrawn)",
        }
        try:
            rr = requests.head(f"{URL}/rest/v1/sale_results",
                               params=params, headers={**H, "Prefer": "count=exact"},
                               timeout=10)
            remaining = int(rr.headers.get("content-range", "0-0/0").split("/")[-1])
            print(f"\n  Còn {remaining} lots {args.source} cần pull "
                  f"(~{(remaining + args.limit - 1) // args.limit} lần chạy "
                  f"nữa với --limit {args.limit}).")
        except Exception as e:
            print(f"  (couldn't count remaining: {e})")
    if not args.probe:
        print("\nRefreshing artist stats…")
        import subprocess
        subprocess.run(["python3", str(ROOT / "supabase" / "refresh_artist_stats.py")], check=False)


if __name__ == "__main__":
    main()
