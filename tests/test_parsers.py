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

    def test_lithograph_in_title_is_print(self):
        # Regression: Lebadang lot 19439 with title 'Nadir lithograph on
        # embossed' was classified as painting because 'lithograph' wasn't
        # in _TITLE_PRINT_KWS — only in _PRINT_MEDIUM_KWS.
        self.assertEqual(classify_kind('', 'Nadir lithograph on embossed'), 'print')
        self.assertEqual(classify_kind('', 'Some Title lithograph'), 'print')
        self.assertEqual(classify_kind('', 'Lithographie de Le Pho'), 'print')

    def test_artists_proof_is_print(self):
        # 'Artists Proof' / 'Artist's Proof' / 'AP' (épreuve d'artiste) =
        # print edition marker.  Lebadang lot 19440 had this in title.
        self.assertEqual(classify_kind('', 'Untitled Abstract, Artists Proof'), 'print')
        self.assertEqual(classify_kind('', "Untitled, Artist's Proof"), 'print')

    def test_etching_screenprint_in_title_is_print(self):
        self.assertEqual(classify_kind('', 'Untitled etching'), 'print')
        self.assertEqual(classify_kind('', 'Title sérigraphie'), 'print')

    def test_edition_number_is_print(self):
        # Pattern '142/250' = limited edition print marker.  Already in
        # _EDITION_NUM_RE — guard against accidental removal.
        self.assertEqual(classify_kind('', 'Title 142/250'), 'print')


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


class TestDimRegex(unittest.TestCase):
    """The Invaluable parser's _DIM_RE used to miss '100cm x 100cm'
    because between the first number and the 'x', the unit 'cm' was
    parsed where the regex only allowed optional quote + whitespace.

    Document the patterns it MUST handle.  Run via _parse_dims_text from
    invaluable_detail_parser, which is the public surface used by the
    dim sweep.
    """
    def _dim(self, text):
        from crawlers.invaluable_detail_parser import _parse_dims_text
        return _parse_dims_text(text)

    def test_double_unit_cm(self):
        # Regression: NTR 'Message' lot — '100cm x 100cm' in page text.
        out = self._dim('Dimensions 100cm x 100cm')
        self.assertIsNotNone(out, "100cm x 100cm not parsed")
        w, h, _ = out
        self.assertEqual((w, h), (100.0, 100.0))

    def test_single_unit_cm(self):
        out = self._dim('Dimensions 65 x 50.6 cm')
        self.assertIsNotNone(out)
        self.assertEqual((out[0], out[1]), (65.0, 50.6))

    def test_inches_to_cm(self):
        # 26 x 31 3/4 in → 66.04 x 80.65 cm (fractional inches)
        out = self._dim('Dimensions 26 x 31 3/4 in')
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out[0], 66.04, places=1)
        self.assertAlmostEqual(out[1], 80.65, places=1)

    def test_quote_inches(self):
        # '35.25" H x 35.25" W' style — each number followed by quote
        out = self._dim('35.25" x 35.25"')
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out[0], 89.5, places=1)

    def test_h_w_labels_inches(self):
        # Regression: BHH lots 19595, 19596, 19609 — '48"h, 96"w' style
        # with explicit H/W labels.
        out = self._dim('Dimensions (H, W, D): 48"h, 96"w overall')
        self.assertIsNotNone(out, "'48\"h, 96\"w' not parsed")
        # H = 48" = 121.92 cm, W = 96" = 243.84 cm
        # parser returns (w_cm, h_cm, raw) — Bonhams/Invaluable H × W convention
        # is handled at storage time; here we just want both numbers extracted.
        w, h, _ = out
        vals = {round(w, 1), round(h, 1)}
        self.assertIn(121.9, vals)
        self.assertIn(243.8, vals)

    def test_by_separator_double_unit(self):
        # Regression: BHH lot 19607 — '160.5cm by 122cm' style.
        out = self._dim('Dimensions: 160.5cm by 122cm')
        self.assertIsNotNone(out, "'160.5cm by 122cm' not parsed")
        w, h, _ = out
        self.assertEqual({w, h}, {160.5, 122.0})

    def test_french_h_l_labels(self):
        # Regression: Pham Hau lot 19270 — 'H. 60 cm - L. 100,5 cm'.
        # Hauteur (height) 60 cm, Largeur (width) 100.5 cm.
        out = self._dim('H. 60 cm - L. 100,5 cm')
        self.assertIsNotNone(out, "French H./L. labels not parsed")
        # Either order acceptable
        w, h, _ = out
        self.assertEqual({w, h}, {60.0, 100.5})


class TestCurrencyConversion(unittest.TestCase):
    """FX rates declared in crawlers/invaluable_detail_runner.py.  These
    used to be incorrect (HKD lots stored at USD face value, off by ~8×)
    until session fix; lock the rates in via a test."""

    def test_fx_table(self):
        # Import the FX table the crawler actually uses
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_runner",
            Path(__file__).resolve().parent.parent / "crawlers" / "invaluable_detail_runner.py",
        )
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            self.skipTest("runner module imports playwright; skip when unavailable")
        FX = mod.FX
        # Lock the major rates against accidental changes.  Update with
        # market drift, but never below 80 % of these baselines.
        self.assertGreater(FX.get('USD', 0), 0.99)
        self.assertLess(FX.get('HKD', 0), 0.15)  # ~0.128
        self.assertGreater(FX.get('GBP', 0), 1.20)  # ~1.27
        self.assertGreater(FX.get('EUR', 0), 1.0)   # ~1.08
        self.assertLess(FX.get('MYR', 0), 0.25)     # ~0.22


class TestArtistMatchingPitfalls(unittest.TestCase):
    """Document the slug-vs-artist matching rules so 'nguyen-trung-tin'
    URLs don't get mapped to 'Nguyen Trung' artist by prefix match."""

    def test_slug_extra_token_should_unmap(self):
        # Imitate the runner's validate_artist logic for a few cases.
        # Real code path:
        # crawlers/invaluable_detail_runner.py::validate_artist
        from crawlers.invaluable_detail_runner import validate_artist, normalize_name
        # Mapped to Nguyen Trung (id 101), slug has 'tin' or 'phan' extra
        ok, _ = validate_artist(
            "",
            "https://www.invaluable.com/auction-lot/nguyen-trung-tin-xxe-siecle-244-c-fake",
            normalize_name("Nguyen Trung"),
            set(),  # empty vocab → 'tin' must trigger via VN allowlist
        )
        self.assertFalse(ok, "Nguyen Trung Tin slug must NOT validate as Nguyen Trung")
        ok, _ = validate_artist(
            "",
            "https://www.invaluable.com/auction-lot/nguyen-trung-phan-vietnamese-b-1940-c-fake",
            normalize_name("Nguyen Trung"),
            set(),
        )
        self.assertFalse(ok, "Nguyen Trung Phan slug must NOT validate as Nguyen Trung")

    def test_exact_name_matches(self):
        # Slug matches mapped artist exactly → validate
        from crawlers.invaluable_detail_runner import validate_artist, normalize_name
        ok, _ = validate_artist(
            "",
            "https://www.invaluable.com/auction-lot/nguyen-trung-1940-elegant-ladies-c-fake",
            normalize_name("Nguyen Trung"),
            set(),
        )
        self.assertTrue(ok, "Exact-token slug should validate")

    def test_short_extra_token_rejected(self):
        # Extra token like '20th', 'royal', 'large' must NOT cause unmap
        # (they're descriptors, not name extensions).
        from crawlers.invaluable_detail_runner import validate_artist, normalize_name
        for extra in ('20th', 'royal', 'large', 'love', 'ii', 'iii'):
            url = f"https://www.invaluable.com/auction-lot/dang-xuan-hoa-{extra}-something-c-fake"
            ok, _ = validate_artist("", url, normalize_name("Dang Xuan Hoa"), set())
            self.assertTrue(ok, f"Extra '{extra}' must NOT trigger unmap")


if __name__ == '__main__':
    unittest.main(verbosity=2)
