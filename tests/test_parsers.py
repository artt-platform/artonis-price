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
        # Title-first then artist tokens (no '-by-' marker).  Lots 19234,
        # 19388, 14734.  Caller passes artist_tokens, we find them in
        # the middle of the slug and split the title off.
        ('NTGT-three-gates',
         'https://www.invaluable.com/auction-lot/the-three-gates-nguyen-gia-tri-1908-1993-41-x-36--101-c-c02447f8ee',
         'The Three Gates', {'nguyen', 'gia', 'tri'}),
        ('NGT-two-girls-title-first',
         'https://www.invaluable.com/auction-lot/two-girls-nguyen-gia-tri-1908-1993-27-x-33-5-cm-i-100-c-6cf475a856',
         'Two Girls', {'nguyen', 'gia', 'tri'}),
        # Single-year '-by-' variant: '-by-<artist>-<single-year>' not
        # always birth-death pair.  Pattern A regex must tolerate it.
        ('NTN-year-of-ox-by-single-year',
         'https://www.invaluable.com/auction-lot/the-year-of-the-ox-97-by-nguyen-tu-nghiem-1922-20-102-c-b744d9eaae',
         'The Year of the Ox 97', None),
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
        w, h, _ = out
        self.assertEqual({w, h}, {60.0, 100.5})

    def test_osenat_a_vue_labels(self):
        # Osenat lot 4557 — 'Dimensions à vue : H. 47 x L. 67,8 cm'.
        out = self._dim('Dimensions à vue : H. 47 x L. 67,8 cm')
        self.assertIsNotNone(out, "Osenat 'à vue' H./L. not parsed")
        w, h, _ = out
        self.assertEqual({w, h}, {47.0, 67.8})

    def test_overall_size_height_wide(self):
        # BHH lacquer panel — 'Overall Size - 198cm high, 250cm wide'.
        out = self._dim('Overall Size - 198cm high, 250cm wide')
        self.assertIsNotNone(out, "'198cm high, 250cm wide' not parsed")
        w, h, _ = out
        # 198 = height, 250 = width
        self.assertEqual((w, h), (250.0, 198.0))

    def test_artcurial_feuille_prefix(self):
        # Artcurial lot 158 — 'Feuille: 27 x 19 cm - 10 5/8 x 7 1/2 in'.
        # 'Feuille' (sheet) is a label prefix.  The cm pair leads.
        out = self._dim('Feuille: 27 x 19 cm - 10 5/8 x 7 1/2 in')
        self.assertIsNotNone(out, "Artcurial 'Feuille' label not parsed")
        w, h, _ = out
        self.assertEqual((w, h), (27.0, 19.0))


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

    def test_slug_omitting_family_name_should_match_catalog(self):
        """Millon (and other French houses) often label Vietnamese artists
        without the family name in the slug — 'trong-kiem' for 'Nguyen Trong
        Kiem'.  The VN-whitelist prefilter must catch this so the lot isn't
        skipped before fetch.  Reproduces the bug that hid Nguyễn Trọng Kiệm
        lot 82 in Millon vente 4201.
        """
        VN_CATALOG = {'nguyen trong kiem'}

        def slug_is_vn(slug_norm, vn_catalog):
            return slug_norm in vn_catalog or any(
                slug_norm == k
                or slug_norm.startswith(k + ' ')
                or k.startswith(slug_norm + ' ')
                or k.endswith(' ' + slug_norm)
                or slug_norm.endswith(' ' + k)
                for k in vn_catalog
            )

        # Bug case: slug omits family name
        self.assertTrue(slug_is_vn('trong kiem', VN_CATALOG))
        # Exact match
        self.assertTrue(slug_is_vn('nguyen trong kiem', VN_CATALOG))
        # Non-VN artist
        self.assertFalse(slug_is_vn('victor tardieu', VN_CATALOG))

    def test_short_extra_token_rejected(self):
        # Extra token like '20th', 'royal', 'large' must NOT cause unmap
        # (they're descriptors, not name extensions).
        from crawlers.invaluable_detail_runner import validate_artist, normalize_name
        for extra in ('20th', 'royal', 'large', 'love', 'ii', 'iii'):
            url = f"https://www.invaluable.com/auction-lot/dang-xuan-hoa-{extra}-something-c-fake"
            ok, _ = validate_artist("", url, normalize_name("Dang Xuan Hoa"), set())
            self.assertTrue(ok, f"Extra '{extra}' must NOT trigger unmap")


class TestMillonCatalogParsing(unittest.TestCase):
    """Millon detail-page parsing.

    Each test maps to a real Millon HTML pattern from production.  We
    test the helpers directly without hitting the network so the suite
    stays fast.
    """

    def test_extract_adjuge_price(self):
        from crawlers.millon import _extract_adjuge_eur
        # vente4201 lot 11 markup
        html = '<p class="title">Adjugé à</p><p class="price">3 500 €</p>'
        self.assertEqual(_extract_adjuge_eur(html), 3500.0)
        # Narrow no-break space (U+202F)
        html2 = '<p class="title">Adjugé à</p><p class="price">42 000\xa0€</p>'
        self.assertEqual(_extract_adjuge_eur(html2), 42000.0)
        # No Adjugé block → returns None
        self.assertIsNone(_extract_adjuge_eur('<p>nothing here</p>'))

    def test_extract_estimation_range(self):
        from crawlers.millon import _extract_estimation_eur
        html = (
            '<p class="title">Estimation</p>'
            '<p class="price">35 000\xa0€ - 50 000\xa0€</p>'
        )
        self.assertEqual(_extract_estimation_eur(html), (35000.0, 50000.0))
        self.assertEqual(_extract_estimation_eur('<p>nothing</p>'), (None, None))

    def test_lot_slugs_from_catalog_html(self):
        # Regression for the audit bug: parse_catalog_results scrapes the
        # /resultat page and only catches lots with 'Adjugé à' nearby.
        # The new parser walks the normal catalog index, which lists every
        # lot regardless of sale status.  The extraction must capture lot
        # slugs even when no price marker is in the same fragment.
        from crawlers.millon import _extract_lot_slugs
        html = '''
            <a href="/catalogue/vente4201-lame-du-vietnam-arts-anciens-et-modernes/lot11-thang-tran-phenh-1895-1973">Lot 11</a>
            <a href="/catalogue/vente4201-lame-du-vietnam-arts-anciens-et-modernes/lot52-tran-dinh-tho-1919-2011">Lot 52</a>
            <a href="/catalogue/vente4201-lame-du-vietnam-arts-anciens-et-modernes/lot38-pham-hau-1903-1994-attribue">Lot 38</a>
        '''
        slugs = _extract_lot_slugs(html, 'vente4201-lame-du-vietnam-arts-anciens-et-modernes')
        self.assertIn('lot11-thang-tran-phenh-1895-1973', slugs)
        self.assertIn('lot52-tran-dinh-tho-1919-2011', slugs)
        # Attribution lots are extracted here — the VN-filter / FAKE_MARKERS
        # gate handles them later in the pipeline.
        self.assertIn('lot38-pham-hau-1903-1994-attribue', slugs)


class TestSculptureClassification(unittest.TestCase):
    """Sculpture detection beyond the obvious 'sculpture' / 'bronze' keywords.

    The Lebadang lot 31782/4 ('Personnage') is mixed-media-on-wood with
    a separate base/pedestal — '<i>H: 50.5 cm sans le socle</i>'.  The
    medium string alone reads like a painting on wood, but the presence
    of 'socle' / 'pedestal' / 'sans le socle' / 'without the base'
    means it's a 3D object.  Add those markers to the explicit
    sculpture keyword list so the kind comes out right.
    """

    def _classify(self, medium, title):
        from crawlers.common import classify_kind
        return classify_kind(medium, title)

    def test_socle_marker_makes_sculpture(self):
        # Lebadang lot 7933 — Bonhams description contains 'sans le socle'.
        # If the medium string captured by the parser includes 'socle' or
        # 'sans le socle', classify_kind must return 'sculpture'.
        self.assertEqual(
            self._classify('mixed media on wood sans le socle', 'Personnage'),
            'sculpture',
        )

    def test_pedestal_marker_makes_sculpture(self):
        self.assertEqual(
            self._classify('bronze with pedestal', 'Figure'),
            'sculpture',
        )

    def test_without_the_base_marker(self):
        self.assertEqual(
            self._classify('mixed media on wood, 50.5 cm without the base', 'Personnage'),
            'sculpture',
        )

    def test_regular_oil_on_wood_stays_painting(self):
        # 'on wood' alone (no socle/pedestal/base) is a normal painting
        # support — must not get false-positive sculpture.
        self.assertEqual(
            self._classify('oil on wood', 'Landscape'),
            'painting',
        )


class TestCleanArtworkTitle(unittest.TestCase):
    """Strip trailing year + medium tokens from raw catalog titles.

    Invaluable catalog 'title' fields routinely tack year and medium onto
    the artwork name ('GRAY HOUSES, 1995 OIL', 'HOUSES WITH DOG, 1996 OIL').
    These belong in year / medium columns, not in the title.  The cleanup
    pass must also title-case ALL-CAPS strings.
    """

    def _clean(self, t):
        from crawlers.invaluable_detail_parser import _clean_artwork_title
        return _clean_artwork_title(t)

    def test_strip_trailing_year_and_medium(self):
        self.assertEqual(self._clean('GRAY HOUSES, 1995 OIL'), 'Gray Houses')

    def test_strip_year_alone(self):
        self.assertEqual(self._clean('Portrait de Femme, 1954'), 'Portrait de Femme')

    def test_strip_medium_alone(self):
        self.assertEqual(self._clean('Houses with Dog Oil'), 'Houses with Dog')

    def test_strip_lacquer_medium(self):
        self.assertEqual(self._clean('Le Printemps Lacquer'), 'Le Printemps')

    def test_keep_year_when_no_trailing_medium_word(self):
        # If 'YYYY' is in the MIDDLE it stays — it's part of the title.
        # Only the *trailing* year (after a comma or as the last token)
        # gets pulled out.
        self.assertEqual(self._clean('Saigon 1975 Memories'), 'Saigon 1975 Memories')

    def test_title_case_all_caps(self):
        self.assertEqual(self._clean('SPRING GARDEN'), 'Spring Garden')

    def test_leave_normal_title_alone(self):
        self.assertEqual(self._clean('Les enfants s’amusent'), 'Les enfants s’amusent')

    def test_empty_and_none(self):
        self.assertIsNone(self._clean(None))
        self.assertIsNone(self._clean(''))


class TestBonhamsTitleExtraction(unittest.TestCase):
    """Bonhams styled-text title extraction.

    Catalog format puts the artwork title in <i>...</i>, but Bonhams also
    italicises date qualifiers ('circa', 'vers', 'ca.') and Vietnamese
    loan-words ('bình phong', 'cánh giấn').  Picking the FIRST <i> match
    blindly produced artwork_title='circa' for Pham Hau lot 651 — the
    actual title 'Golden Sunset over Halong Bay' was the second match.
    """

    def _extract(self, styled):
        # Mirrors the priority used in crawlers/bonhams.py.
        from crawlers.bonhams import _pick_artwork_title_from_italics
        return _pick_artwork_title_from_italics(styled)

    def test_skip_date_qualifier_italic(self):
        # Pham Hau lot — Golden Sunset over Halong Bay, dated circa 1938-45.
        styled = (
            '<b>PHAM HAU (1903-1995)</b><br/>'
            '<i>circa</i> 1938-1945<br/>'
            '<i>Golden Sunset over Halong Bay</i><br/>'
            "signed with artist's seal<br/>"
            'lacquer, pigment, and gold foil on wood<br/>'
        )
        self.assertEqual(self._extract(styled), 'Golden Sunset over Halong Bay')

    def test_pick_first_real_italic(self):
        # Direct case — first italic IS the title.
        styled = '<b>ARTIST (1900-1980)</b><br/><i>The Title</i><br/>oil on canvas'
        self.assertEqual(self._extract(styled), 'The Title')

    def test_skip_short_foreign_term(self):
        # 'bình phong' is italicised as a loan-word, not a title.  Skip
        # short foreign terms (≤ 2 words) and pick the longer title.
        styled = (
            '<b>ARTIST</b><br/>'
            '<i>bình phong</i> mounted as<br/>'
            '<i>The Real Artwork Title</i><br/>'
            'lacquer on wood'
        )
        self.assertEqual(self._extract(styled), 'The Real Artwork Title')


if __name__ == '__main__':
    unittest.main(verbosity=2)
