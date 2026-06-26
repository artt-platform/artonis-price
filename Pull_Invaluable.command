#!/bin/bash
# Invaluable hammer pull — opens Chrome window (real Chrome, not
# Playwright's Chromium) to defeat Cloudflare's bot check.
# Uses INVALUABLE_COOKIE from .env.local (~30 day validity).
# Double-click in Finder to run.

cd "$(dirname "$0")"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Artonis hammer puller — Invaluable"
echo "  Pulling 10 lots via Playwright (real Chrome)"
echo ""
echo "  A Chrome window will pop up — let it run.  Each lot takes"
echo "  30-90s (Cloudflare challenge + rate-limit spacing)."
echo "  Total time: ~10-15 minutes."
echo "════════════════════════════════════════════════════════════"
echo ""

/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
  supabase/pull_hammers_local.py --source invaluable --limit 10

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Done.  If most lots show 'Cloudflare challenge':"
echo "    - Cookie may have expired.  Open invaluable.com in"
echo "      Chrome, login, then DevTools Network tab → any"
echo "      request to invaluable.com → Request Headers → copy"
echo "      'cookie' value → paste into .env.local replacing"
echo "      INVALUABLE_COOKIE=..."
echo "    - Or CF may have rate-limited your IP — wait 2-4 hours"
echo "      and retry."
echo "════════════════════════════════════════════════════════════"
echo ""

read -n 1 -s -r -p "Press any key to close..."
echo ""
