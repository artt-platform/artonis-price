"""Open a known Sothebys lot in Playwright with login cookie and
record EVERY response.  Print/save the responses whose body contains
the hammer ('5,080,000' or 'Lot Sold' literal).  Tells us which
endpoint actually delivers the hammer — guessing /bsp-api wasted 3
iterations.

Run:
  python3 supabase/discover_sothebys_endpoint.py
"""
from __future__ import annotations
import os, sys, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


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
COOKIE = os.environ.get("SOTHEBYS_COOKIE", "")


def _parse_cookie_string(cookie_str: str, domain: str) -> list[dict]:
    cookie_str = " ".join(cookie_str.split()).strip()
    out = []
    for piece in cookie_str.split(";"):
        piece = piece.strip()
        if "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        out.append({
            "name": name.strip(), "value": value.strip(),
            "domain": domain, "path": "/", "secure": True,
            "httpOnly": False, "sameSite": "Lax",
        })
    return out


# Known-hammer lot for discovery (operator-verified HKD 5,080,000)
LOT_URL = "https://www.sothebys.com/en/buy/auction/2023/modern-day-auction/le-pho-li-pu-two-ladies-liang-wei-shi-nu"
HAMMER_MARKERS = ["5,080,000", "5080000", "5080000.00", "Lot Sold"]


def main() -> None:
    if not COOKIE:
        print("SOTHEBYS_COOKIE missing from .env.local")
        return

    from playwright.sync_api import sync_playwright

    print(f"Opening {LOT_URL}")
    print(f"Looking for: {HAMMER_MARKERS}\n")

    captured = []  # list of (url, status, content_type, body, headers)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        context.add_cookies(_parse_cookie_string(COOKIE, ".sothebys.com"))
        page = context.new_page()

        def on_response(resp):
            try:
                ctype = resp.headers.get("content-type", "")
            except Exception:
                ctype = ""
            # Only inspect things that might be data (skip images/fonts/css/js)
            if not any(s in ctype.lower() for s in ("json", "html", "javascript", "text/plain", "graphql")):
                return
            try:
                body = resp.text()
            except Exception:
                return
            if not body:
                return
            # Does body contain any hammer marker?
            hits = [m for m in HAMMER_MARKERS if m in body]
            if not hits:
                return
            captured.append({
                "url": resp.url, "status": resp.status,
                "content_type": ctype, "body_len": len(body),
                "hits": hits, "body": body,
            })

        page.on("response", on_response)

        try:
            page.goto(LOT_URL, timeout=45_000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"goto failed: {e}")
            browser.close()
            return

        # Give the page extra time — AJAX may fire late
        page.wait_for_timeout(15_000)
        browser.close()

    print(f"\nCaptured {len(captured)} response(s) containing a hammer marker:")
    for i, r in enumerate(captured, 1):
        print(f"\n--- [{i}] ---")
        print(f"  url:           {r['url']}")
        print(f"  status:        {r['status']}")
        print(f"  content-type:  {r['content_type']}")
        print(f"  body length:   {r['body_len']} chars")
        print(f"  marker hits:   {r['hits']}")
        # Show a small window around the first marker
        body = r["body"]
        first_marker = r["hits"][0]
        idx = body.find(first_marker)
        if idx >= 0:
            window = body[max(0, idx - 150): idx + 200]
            print(f"  context: …{window!r}…")
        # Save full body
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", r["url"].split("?")[0])[-60:]
        out = ROOT / f"sothebys_discover_{i}_{slug}.txt"
        out.write_text(body)
        print(f"  saved:         {out.name}")

    if not captured:
        print("\n  ⚠  No response contained any hammer marker.")
        print("  Either the page never loaded the hammer, or the markers don't match.")
        print("  Try opening the URL in your real Chrome → DevTools Network tab,")
        print("  search for '5,080,000' across responses, and tell me the URL of the hit.")


if __name__ == "__main__":
    main()
