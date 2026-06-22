"""Deep data-quality audit — runs against the full sale_results table and
reports every suspect row.  Unlike test_data_invariants.py (which only
asserts), this script tries to find issues we haven't named yet.

Categories scanned:
  1. Title quality:  empty / garbage / contains artist name / contains dim
  2. Dim quality:    null when title/medium contains a parseable pattern
  3. Kind:           painting with lithograph/print/etching/litho in title
  4. Support:        medium says X but support_type says Y (or null)
  5. Furniture:      title contains 'table', 'chair', 'cabinet' but kind=painting
  6. Attribution:    title has Attributed / Attr. / Atelier de / Cercle de
                     but row is still mapped to an artist
  7. Stale $/m²:     non-painting kind but price_per_m2_usd is not null
  8. Currency:       price_usd looks like raw HKD/EUR (large round numbers)

Run:  python3 tests/deep_audit.py
"""
import sys
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawlers.common import classify_kind, detect_support_type
from crawlers.invaluable_detail_parser import _parse_dims_text

ENV_PATH = Path(__file__).resolve().parent.parent / '.env.local'
ENV = {}
for line in ENV_PATH.read_text().splitlines():
    line = line.strip()
    if line and '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        ENV[k] = v
URL = ENV['SUPABASE_URL']
KEY = ENV['SUPABASE_SERVICE_ROLE_KEY']
HR = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}


def _deaccent(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn').lower()


def _paginate(extra):
    out = []
    offset = 0
    while True:
        rs = requests.get(
            f"{URL}/rest/v1/sale_results?{extra}&offset={offset}&limit=1000",
            headers=HR,
        ).json()
        if not isinstance(rs, list) or not rs:
            break
        out.extend(rs)
        offset += len(rs)
        if len(rs) < 1000:
            break
    return out


# Attribution requires the marker AT THE START of the title with the
# explicit catalog phrasing.  Drop 'circle of' / 'after X' / 'manner of' —
# they cause false positives ('Circle of Life', 'After the Storm') and
# real attribution lots almost always use the explicit forms below.
ATTRIBUTION_RE = re.compile(
    r'^(?:attribut(?:é|ed)\s*(?:to|à)|attr\.?\s*to|'
    r'atelier\s+de|école\s+de|d[\'’]\s*après|entourage\s+de|suiveur\s+de|'
    r'follower\s+of)\b',
    re.IGNORECASE,
)

PRINT_HINT_RE = re.compile(
    r'\b(lithograph|lithographie|sérigraphie|serigraphie|screenprint|'
    r'etching|engraving|gravure|artists?\s+proof|épreuve\s+d[\'’]?artiste)\b',
    re.IGNORECASE,
)

# A FURNITURE lot title usually has the furniture word at the start or as
# the dominant noun — 'Coffee Table', 'Lacquer Cabinet', 'Console'.  Titles
# like 'Fleurs sur la chaise' (flowers on the chair) describe a painting,
# not a chair.  Anchor at start to avoid the false positives.
FURNITURE_RE = re.compile(
    r'^(?:coffee\s+table|console\s+table|side\s+table|low\s+table|'
    r'lacquer\s+table|lacquer\s+cabinet|lacquer\s+coffre|'
    r'(?:large\s+)?lacquer\s+(?:box|dish)|cabinet|commode|armoire|coffre)\b',
    re.IGNORECASE,
)


def main():
    rows = _paginate('artist_id=not.is.null&select=id,source,source_url,artist_id,'
                     'artist_name_raw,artwork_title,medium,dimensions,width_cm,height_cm,'
                     'area_m2,price_usd,price_with_premium_usd,price_per_m2_usd,kind,support_type')
    print(f"Auditing {len(rows)} mapped lots\n")

    # Get artist names for name-leak detection
    artists = requests.get(
        f"{URL}/rest/v1/artists?select=id,name", headers={**HR, 'Range': '0-999'}
    ).json()
    NAMES = {a['id']: a['name'] for a in artists}

    issues = defaultdict(list)

    for r in rows:
        title = (r.get('artwork_title') or '').strip()
        medium = (r.get('medium') or '').strip()
        kind = r.get('kind')
        ppm = r.get('price_per_m2_usd')
        artist_name = NAMES.get(r['artist_id'], '')

        # 1. Title quality
        if not title:
            issues['title_empty'].append(r['id'])
        elif re.match(r'^\d+(\.\d+)?\s*x\s*\d+(\.\d+)?\s*cm\s*$', title, re.IGNORECASE):
            issues['title_is_dim'].append(r['id'])
        elif re.match(r'^\(\s*\w+,?\s*\d{4}.*\)\s*$', title):
            issues['title_is_metadata'].append(r['id'])
        elif artist_name and len(title) < len(artist_name) + 3 and \
             _deaccent(artist_name).replace(' ', '') in _deaccent(title).replace(' ', ''):
            issues['title_is_artist_name'].append(r['id'])

        # 2. Dim missing despite title/medium having pattern
        if not r.get('width_cm') and not r.get('height_cm'):
            for source_field in [title, medium]:
                if not source_field:
                    continue
                try:
                    out = _parse_dims_text(source_field)
                except Exception:
                    continue
                if out:
                    issues['dim_missing_but_parseable'].append((r['id'], source_field[:60]))
                    break

        # 3. Kind=painting but title has print hint
        if kind == 'painting' and PRINT_HINT_RE.search(title + ' ' + medium):
            new_kind = classify_kind(medium, title)
            if new_kind != 'painting':
                issues['painting_should_be_print'].append((r['id'], new_kind, title[:60]))

        # 4. Furniture mistakenly tagged as painting
        if kind == 'painting' and FURNITURE_RE.search(title):
            issues['furniture_as_painting'].append((r['id'], title[:60]))

        # 5. Stale $/m² for non-paintings
        if ppm is not None and kind not in ('painting', None):
            issues['stale_ppm_nonpainting'].append((r['id'], kind))

        # 6. Attribution still mapped
        if ATTRIBUTION_RE.search(title):
            issues['attribution_still_mapped'].append((r['id'], title[:60]))

        # 7. Support_type vs medium mismatch
        if medium:
            expected = detect_support_type(medium, title)
            actual = r.get('support_type')
            if expected and actual and expected != actual:
                issues['support_mismatch'].append((r['id'], medium, expected, actual))

        # 8. Tiny price_usd that smells like raw HKD/EUR (e.g., $5 hammer)
        if r.get('price_usd') and 0 < r['price_usd'] < 50:
            issues['price_under_50'].append((r['id'], r['price_usd'], r['source']))

    # Print summary
    print("=== Issue summary ===")
    for k in sorted(issues):
        lst = issues[k]
        print(f"  {k:35s}  {len(lst)}")
        for item in lst[:3]:
            print(f"      {item}")
        if len(lst) > 3:
            print(f"      ... +{len(lst)-3} more")


if __name__ == '__main__':
    main()
