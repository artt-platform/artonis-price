"""Live-DB invariants — runs against Supabase, not deterministic.

These verify the *state* of the data right now.  When they fail it's a
real data issue that needs investigation, not a code bug.  Use this
before+after any bulk patch to catch regressions.

Run: python3 tests/test_data_invariants.py
"""
import os
import re
import sys
import unicodedata
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def _paginate(table, params, extra_headers=None):
    out = []
    offset = 0
    while True:
        rs = requests.get(
            f"{URL}/rest/v1/{table}?{params}&offset={offset}&limit=1000",
            headers={**HR, **(extra_headers or {})},
        ).json()
        if not isinstance(rs, list) or not rs:
            break
        out.extend(rs)
        offset += len(rs)
        if len(rs) < 1000:
            break
    return out


class TestArtistNameRawConsistency(unittest.TestCase):
    """artist_name_raw should reflect the FULL artist string from the source,
    not a truncated prefix that happened to match an existing artist.

    Specific regression: 'nguyen-trung-tin' URL slug — parser stored
    artist_name_raw='Nguyen Trung' (prefix match) instead of the full
    'Nguyen Trung Tin'.  Even after the lot's artist_id was nulled by
    the validate sweep, the raw name stayed wrong and the UI displayed
    it as 'Nguyen Trung' for unmapped lots.
    """

    def test_unmapped_lots_raw_name_matches_slug(self):
        # For lots with artist_id null but a clear URL slug, the raw name
        # should match what the slug suggests — not be a prefix.
        rows = _paginate(
            'sale_results',
            'artist_id=is.null&artist_name_raw=neq.&source_url=ilike.*invaluable.com*&select=id,artist_name_raw,source_url',
        )
        slug_re = re.compile(r'/auction-lot/([a-z0-9\-]+)-\d+-c-[a-f0-9]+', re.IGNORECASE)
        violations = []
        for r in rows:
            m = slug_re.search(r['source_url'] or '')
            if not m:
                continue
            slug = m.group(1)
            # Take the first ~3 dash-tokens before any 4-digit year as
            # the slug's artist hint.
            tokens = []
            for t in slug.split('-'):
                if re.fullmatch(r'\d{4}', t) or t in ('b', 'born', 'vietnamese', 'vietnam', 'french'):
                    break
                tokens.append(t)
                if len(tokens) >= 4:
                    break
            slug_artist = ''.join(tokens)
            raw = _deaccent(r['artist_name_raw']).replace(' ', '').replace('-', '')
            if not slug_artist or not raw:
                continue
            # raw should be at least as long as slug suggests if slug had
            # extra tokens. If raw is strictly shorter and a prefix of
            # slug, that's the regression.
            if len(raw) < len(slug_artist) and slug_artist.startswith(raw):
                violations.append((r['id'], r['artist_name_raw'], slug))
        if violations:
            sample = '\n  '.join(f"id={v[0]} raw={v[1]!r} slug={v[2]!r}" for v in violations[:5])
            self.fail(
                f"{len(violations)} unmapped Invaluable lots have artist_name_raw as a strict prefix "
                f"of the slug — likely truncated by prefix-match at insert time.\n  {sample}"
            )


class TestPriceAreaConsistency(unittest.TestCase):
    """price_per_m2_usd uses price_with_premium_usd when present, else
    price_usd (see compute_area_and_price_per_m2 in artonis_price_mvp).
    Test the same formula.
    """

    def test_ppm2_matches_price_over_area(self):
        rows = _paginate(
            'sale_results',
            'price_per_m2_usd=not.is.null&area_m2=gt.0&select=id,price_usd,price_with_premium_usd,area_m2,price_per_m2_usd',
        )
        bad = []
        for r in rows:
            basis = r.get('price_with_premium_usd') or r.get('price_usd')
            if not basis or basis <= 0:
                continue
            expected = basis / r['area_m2']
            actual = r['price_per_m2_usd']
            if abs(expected - actual) / max(expected, 1) > 0.05:  # >5% off
                bad.append((r['id'], basis, r['area_m2'], actual, round(expected)))
        if bad:
            sample = '\n  '.join(
                f"id={b[0]} \${b[1]:.0f}/{b[2]}m² → stored \${b[3]:.0f}, expected ~\${b[4]}"
                for b in bad[:5]
            )
            self.fail(f"{len(bad)} rows have stale price_per_m2_usd.\n  {sample}")


class TestDimAreaConsistency(unittest.TestCase):
    """area_m2 should equal width_cm × height_cm / 10000 (within 1%)."""

    def test_area_matches_dims(self):
        rows = _paginate(
            'sale_results',
            'width_cm=not.is.null&height_cm=not.is.null&area_m2=not.is.null&select=id,width_cm,height_cm,area_m2',
        )
        bad = []
        for r in rows:
            expected = r['width_cm'] * r['height_cm'] / 10000
            if abs(expected - r['area_m2']) / max(expected, 0.0001) > 0.05:
                bad.append((r['id'], r['width_cm'], r['height_cm'], r['area_m2'], round(expected, 4)))
        if bad:
            sample = '\n  '.join(f"id={b[0]} {b[1]}×{b[2]}cm → stored {b[3]}, expected {b[4]}" for b in bad[:5])
            self.fail(f"{len(bad)} rows have stale area_m2.\n  {sample}")


class TestArtistIdConsistency(unittest.TestCase):
    """Every artist_id should resolve to an existing artists row."""

    def test_no_dangling_artist_id(self):
        # Get all distinct artist_ids from sale_results
        # Use PostgREST aggregation via 'select=artist_id&order=artist_id'
        rows = _paginate('sale_results', 'artist_id=not.is.null&select=artist_id&order=artist_id')
        sale_ids = {r['artist_id'] for r in rows}
        artist_rows = _paginate('artists', 'select=id')
        artist_ids = {a['id'] for a in artist_rows}
        dangling = sale_ids - artist_ids
        if dangling:
            self.fail(f"{len(dangling)} sale_results rows reference non-existent artists: {sorted(dangling)[:10]}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
