#!/bin/bash
# Sothebys hammer pull — no Chrome window, ~1s per lot.
# Uses SOTHEBYS_BEARER from .env.local (Auth0 JWT, ~24h validity).
# Double-click in Finder to run.

cd "$(dirname "$0")"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Artonis hammer puller — Sothebys"
echo "  Pulling 20 lots via direct GraphQL"
echo "════════════════════════════════════════════════════════════"
echo ""

/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
  supabase/pull_hammers_local.py --source sothebys --limit 20

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Done.  If you saw 'Bearer expired' or 401 errors:"
echo "    1. Open sothebys.com in Chrome (any lot page)"
echo "    2. F12 → Network → graphql request → Request Headers"
echo "    3. Copy 'authorization: Bearer ...' value (without 'Bearer ')"
echo "    4. Paste into .env.local, replacing SOTHEBYS_BEARER=..."
echo "════════════════════════════════════════════════════════════"
echo ""

read -n 1 -s -r -p "Press any key to close..."
echo ""
