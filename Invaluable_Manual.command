#!/bin/bash
# Manual Invaluable hammer entry — bypass Cloudflare entirely.
# Open each lot URL in Chrome, read hammer, paste back.
# Double-click in Finder to run.

cd "$(dirname "$0")"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Invaluable manual hammer entry"
echo ""
echo "  Cloudflare won't let automation through, so we do it"
echo "  by hand: open the URL in Chrome (normal, not automation),"
echo "  read the hammer price, type it back here."
echo "════════════════════════════════════════════════════════════"
echo ""

/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
  supabase/import_invaluable_manual.py --limit 10

echo ""
read -n 1 -s -r -p "Press any key to close..."
echo ""
