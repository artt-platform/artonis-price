"""Golden tests for crawler parsing functions.

These cases are the regressions we've actually hit in production — each
test maps to a specific session where the user reported wrong data.  Add
to this file whenever a new parser bug is fixed so the same break can't
sneak back in.

Run: python3 -m tests.test_parsers   (or)   python3 tests/test_parsers.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artonis_price_mvp import parse_dimensions
from crawlers.common import classify_kind, detect_support_type
from crawlers.invaluable_detail_parser import _title_from_invaluable_slug


class TestSlugFallback(unittest.TestCase):
    """Invaluable lot URL slug → artwork title recovery.

    Real lots from the session that user reported as wrong.  Each tuple is
    (lot_id-mnemonic, url, expected_title, artist_tokens or None).
    """
    CASES = [
        ('NGT-spring-garden-by',
         'https://www.invaluable.com/auction-lot/spring-garden-by-nguyen-gia-tri-1908-1993-77-x-57-64-c-572454d8c8',
         'Spring Garden', None),
        ('HVD-lady-with-a-fan',
         'https://www.invaluable.com/auction-lot/hong-viet-dung-b-1962-lady-with-a-fan-55-c-891457dab5',
         'Lady with a Fan', {'hong', 'viet', 'dung'}),
        ('DHP-boat-by-house',
         'https://www.invaluable.com/auction-lot/dao-hai-phong-boat-by-house-o-c-315-c-965490d957',
         'Boat by House', {'dao', 'hai', 'phong'}),
        ('Nguyen-Sang-portrait-1954',
         'https://www.invaluable.com/auction-lot/nguyen-sang-1923-1988-portrait-de-femme-1954-20-c-fake',
         'Portrait de Femme 1954', {'nguyen', 'sang'}),
        ('DXH-no-title-in-slug',
         'https://www.invaluable.com/auction-lot/dang-xuan-hoa-103-c-cf44656a5c',
         None, {'dang', 'xuan', 'hoa'}),
        # TODO: handle 'title-first then artist (no -by- marker)' pattern.
        # URL: the-three-gates-nguyen-gia-tri-1908-1993-41-x-36--101-c-…
        # We can detect 'nguyen-gia-tri' in the middle if artist_tokens are
        # supplied and split the title off — slug-fallback doesn't yet.
        # ('NTGT-three-gates',
        #  'https://www.invaluable.com/auction-lot/the-three-gates-nguyen-gia-tri-1908-1993-41-x-36--101-c-c02447f8ee',
        #  'The Three Gates', {'nguyen', 'gia', 'tri'}),
    ]

    def test_all_cases(self):
        for name, url, expected, tokens in self.CASES:
            with self.subTest(case=name):
                got = _title_from_invaluable_slug(url, tokens)
                self.assertEqual(got, expected, f"slug recovery failed for {name}")


class TestParseDimensions(unittest.TestCase):
    """Per-source H × W vs W × H convention — see CONVENTIONS.md."""

    def test_bonhams_h_w(self):
        # Bonhams text "85 x 63 cm" reads as Height × Width.
        # Expect (width=63, height=85) returned as (w, h).
        w, h = parse_dimensions('85 x 63 cm', source='bonhams')
        self.assertEqual((w, h), (63.0, 85.0))

    def test_sothebys_h_w(self):
        w, h = parse_dimensions('72.5 x 60 cm', source='sothebys')
        self.assertEqual((w, h), (60.0, 72.5))

    def test_invaluable_h_w(self):
        w, h = parse_dimensions('27 x 33.5 cm', source='invaluable')
        self.assertEqual((w, h), (33.5, 27.0))

    def test_le_auction_default(self):
        # Le Auction uses item.width/.height explicit fields — when text
        # is constructed it's already W × H.  No swap.
        w, h = parse_dimensions('40 x 50 cm', source='le_auction')
        self.assertEqual((w, h), (40.0, 50.0))

    def test_christies_default(self):
        # Christie's JSON sets columns directly; text fallback uses W × H.
        w, h = parse_dimensions('65 x 50.6 cm', source='christies')
        self.assertEqual((w, h), (65.0, 50.6))

    def test_unparseable_returns_none(self):
        w, h = parse_dimensions('', source='bonhams')
        self.assertEqual((w, h), (None, None))


class TestClassifyKind(unittest.TestCase):
    """Kind classification — see classify_kind comments in common.py."""

    def test_truu_tuong_is_painting_not_sculpture(self):
        # "Trừu tượng" = abstract painting, despite containing 'tượng'.
        self.assertEqual(classify_kind('', 'Trừu tượng'), 'painting')
        self.assertEqual(classify_kind('', 'Bố cục trừu tượng'), 'painting')
        self.assertEqual(classify_kind('', 'Phong cảnh trừu tượng'), 'painting')

    def test_tuong_alone_is_sculpture(self):
        # Standalone 'tượng' (statue) still classifies as sculpture.
        self.assertEqual(classify_kind('', 'Tượng đài'), 'sculpture')
        self.assertEqual(classify_kind('', 'Bức tượng phụ nữ'), 'sculpture')

    def test_explicit_sculpture(self):
        self.assertEqual(classify_kind('bronze', 'Sculpture en bronze'), 'sculpture')

    def test_lacquer_box_is_sculpture(self):
        # 3D lacquer objects (boxes, dishes) — not 2D paintings.
        self.assertEqual(classify_kind('lacquer', 'lacquer box'), 'sculpture')


class TestDetectSupportType(unittest.TestCase):
    """Support type — affects $/m² peer comparison."""

    def test_carton_is_paper_not_panel(self):
        # 'Huile sur carton' / 'sơn dầu trên bìa cứng' = paper-family.
        self.assertEqual(detect_support_type('Huile sur carton', ''), 'paper')
        self.assertEqual(detect_support_type('sơn dầu trên bìa cứng', ''), 'paper')
        self.assertEqual(detect_support_type('oil on cardboard', ''), 'paper')

    def test_wood_panel_is_panel(self):
        self.assertEqual(detect_support_type('huile sur panneau de bois', ''), 'panel')
        self.assertEqual(detect_support_type('oil on wood', ''), 'panel')
        self.assertEqual(detect_support_type('oil on panel', ''), 'panel')

    def test_silk_canvas_lacquer(self):
        self.assertEqual(detect_support_type('ink on silk', ''), 'silk')
        self.assertEqual(detect_support_type('oil on canvas', ''), 'canvas')
        self.assertEqual(detect_support_type('lacquer on wood', ''), 'lacquer')


if __name__ == '__main__':
    unittest.main(verbosity=2)
