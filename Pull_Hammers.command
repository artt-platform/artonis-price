#!/bin/bash
# Double-click this file in Finder to pull hammer prices.
# Terminal opens, script runs, you see live progress, window stays
# open at the end so you can read the result.

cd "$(dirname "$0")"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Artonis hammer puller"
echo "  Pulling 10 lots each from Sothebys + Invaluable"
echo "════════════════════════════════════════════════════════════"
echo ""

/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
  supabase/pull_hammers_local.py --limit 10

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Done.  Close this window when you're done reading."
echo "════════════════════════════════════════════════════════════"
echo ""

# Hold the window open
read -n 1 -s -r -p "Press any key to close..."
echo ""
