# Crawler & Data Conventions

Reference for every crawler + data-cleanup script.  When in doubt, read this
first — it captures decisions made one-painting-at-a-time over hours of
debugging and is the only place they're written down.

## Dimensions: storage, display, per-source convention

Two columns hold the actual measurements:

- `width_cm` — width of the artwork
- `height_cm` — height of the artwork

Storage of the `dimensions` string is **canonical W × H** (`"width x height cm"`),
matching the order `parse_dimensions` reads.  The UI re-renders **H × W** from
the labelled columns (international/Vietnamese catalog convention).  See
`ArtonisV2/src/app/artists/[id]/page.tsx::formatDimHW` and the parallel copy in
`/sales/page.tsx`.

### Per-source first-number convention

Most catalogues write height first in the text; a few write width first.  This
matters because the dim-string parser otherwise blindly takes the first number
as width.

`_HW_FIRST_SOURCES` in `artonis_price_mvp.py` lists the sources where text
reads **Height × Width**.  `parse_dimensions(text, source=…)` consults the set
and swaps the captured pair when the source belongs.

| Source | Convention | How parser knows |
|---|---|---|
| Bonhams | H × W | in `_HW_FIRST_SOURCES` |
| Sotheby's | H × W | in `_HW_FIRST_SOURCES` |
| Aguttes / Drouot / Gros-Delettrez / Tajan / Artcurial / Millon / Osenat | H × W | in `_HW_FIRST_SOURCES` (French houses all) |
| Invaluable (text fallback) | H × W | in `_HW_FIRST_SOURCES` |
| Christie's JSON | explicit `"height_cm":"…","width_cm":"…"` | parser reads labels directly, bypasses `parse_dimensions` |
| Christie's text fallback (`"(65.0 x 50.6 cm.)"`) | W × H (Christie's labels `W … x H …` in the text it derives from) | parser maps group(1)→W, group(2)→H |
| Le Auction (Bidspirit) | uses `item.width` / `item.height` explicit fields | parser bypasses `parse_dimensions` |

**3D / depth pattern** (`"70 by 130 by 2.5 cm"` — reliefs, lacquer panels): take
the first two numbers, drop the third (depth).  Bonhams 3D regex in
`crawlers/bonhams.py`.

**Old Christie's lots without JSON schema**: triple fallback —
JSON → measurements_txt `(W x H cm.)` → bare text — see `crawlers/christies.py`.

## Source URLs

| Source | URL form to store |
|---|---|
| Le Auction | `https://uk.bidspirit.com/ui/lotPage/leauction/source/catalog/auction/{portalKey}/lot/{item_id}/` — `portalKey` lives in `auction.auctionDays[<dayId>].portalKey` from Bidspirit's `loadAuctionDayCatalog` API.  Do NOT use `leauction.bidspirit.com/#catalog~aid~did~item~id` — that subdomain returns 404 on `/ui/lotPage/...`. |
| Christie's | `https://www.christies.com/en/lot/lot-{lot_id}` — strip `?intObjectID=...&saleid=...` query strings. |
| Bonhams / Sothebys / Aguttes etc | per their native URL pattern |

## Artist mapping

- `artist_name_raw` should hold the FULL string captured from the source,
  never a truncation.
- The Osenat parser used to split on `[,(–\-—]` and take the first chunk; for
  French hyphenated names ("JEAN-MICHEL WILMOTTE") that produced "JEAN" and
  the downstream matcher fuzzy-misassigned 47 unrelated French lots to Võ Lăng
  (id=229).  **Do NOT split on hyphen.**  Use `[,(–—]` (no `\-`).
- Attribution lots ("Attribué à X", "Attributed to X", "Attr. X", "Attr to X",
  "Atelier de X", "Cercle de X", "École de X", "After X", "D'après X",
  "Entourage de X") should be flagged as fake and **not mapped to the
  artist's id**.  See `FAKE_MARKERS` in `crawlers/bonhams.py`.

## Title parsing

Many sources cram artist info + title + medium + dim into one string.  After
parsing, `artwork_title` should never contain:

- The artist's full name (Vietnamese or deaccented form)
- A pure dim ("`27 x 33.5 cm`")
- Just years / metadata ("`(Vietnamese, 1908-1993)`")

If extraction can't produce a clean title (page genuinely has no title),
store `NULL` — the UI shows "—".  Better than garbage.

### Invaluable parser (`crawlers/invaluable_detail_parser.py`)

Order of attempts:
1. H1 → strip "Lot N:" prefix → strip artist name (try Title-case, UPPER, and
   reversed-order variants because "Artist or Maker" may use lastname-firstname
   while H1 has firstname-lastname).
2. Strip leading `(country, year-)` parenthetical.
3. Pull trailing `", YYYY"` into the `year` field.
4. If H1 produced nothing (artist-only H1), fall back to the Description
   section: skip lines matching `^[A-Z…]+\s*\([^)]*\d{4}[^)]*\)\s*$`,
   `^Vietnamese.*\d{4}`, `^Vietnam,?\s*\d{4}-?`, `^\(?b\.?\s*\d{4}\)?`.  Take
   the first remaining line.
5. Final safety: if the resulting title looks like garbage (just `(country,
   YYYY-YYYY)`, all-caps name, or pure dim), clear it.

### Slug-fallback for `dim-as-title` URLs

When the parser leaves `artwork_title` as pure dims, recover from the URL slug
**only** when the slug has the explicit `-by-<artist>-<years>` marker.
Without `-by-`, slugs put artist and title in the same segment and we can't
safely split.  See `_title_from_invaluable_slug`.

### LE Auction (`crawlers/le_auction.py`)

The `name` field crams everything: `ARTIST_NAME (years) "Title", 27 x 33 cm,
medium`.  Strip artist + years from the prefix before storing.

## Support type ($/m² peer group)

`detect_support_type` in `crawlers/common.py::_SUPPORT_PATTERNS`.

Critical: cardboard is **paper**-family, not wood — `carton`, `cardboard`,
`bìa cứng` go in the `paper` bucket.  Wood / panel materials (`panneau`,
`bois`, `wood`, `gỗ`, `masonite`, `isorel`) go in `panel`.

Order matters: more-specific first (lacquer → silk → canvas → paper → panel
→ metal).

## Kind classification

`classify_kind` in `crawlers/common.py`.

Pitfall: `"trừu tượng"` (= abstract painting) contains `"tượng"` (= statue),
but it must classify as **painting**, not sculpture.  Both the explicit and
title-based sculpture loops skip the `"tượng"` match when `"trừu tượng"` /
`"tru tuong"` is in the blob.

## Auction-house display name (display vs canonical)

Some auction houses appear on Invaluable under a different brand (proxy /
aggregator).  Examples:

- Cadmore Auctions on Invaluable = Le Auction (Vietnam).  Display the name
  exactly as Invaluable shows it ("Cadmore Auctions via Invaluable") — that's
  what buyers see on Invaluable.
- For lots scraped directly from `leauction.bidspirit.com`, the display is
  "Le Auction".  Both can coexist for the same physical sale.

When the same physical lot appears in both sources (same artist + same date +
exact dim match including W↔H swap), prefer the Invaluable copy (higher
visibility; native Le Auction visitors are rare).  See the cross-source dedup
pass — but be conservative; only delete when dim+date+artist match exactly.

## Sale location → city normalization (for report)

Stored `sale_location` is the raw string from the source; it mixes city,
country, and auction-house name.  The report page normalizes via
`normalizeLocationToCity()` in `ArtonisV2/src/app/report/page.tsx`.  The map
includes (non-exhaustive):

- Hong Kong / Hong Kong SAR / Hong Kong SAR, China → Hong Kong
- Cadmore Auctions / Global Auction / Hà Nội / Paris → Hà Nội (VN sources)
- Christie's / Sotheby's / 33 Auction → Hong Kong (for VN-art context)
- Gros-Delettrez / Artvisory / OXIO / Akiba / Pays de Fayence Enchères → Paris
  (or specific French city if known)
- KLAS Art Auction / Henry Butcher Art Auctioneers → Kuala Lumpur
- Shapiro / Aalders / etc → Sydney / Auckland

Add new entries when you see new sources surface in the report.

## Pricing & currency

- `hammer_price` + `currency` = native amount, native currency, as written
  on the lot page.
- `price_with_premium` = if the house publishes it; else null.
- `price_usd` = `hammer_price` converted via `FX` table in
  `crawlers/invaluable_detail_runner.py`.  FX rates set there: HKD 0.128, GBP
  1.27, EUR 1.08, MYR 0.22, SGD 0.74, TWD 0.032.  Update when rates drift far.
- `price_with_premium_usd` = explicit premium-included USD if available, else
  derived from `price_usd × (1 + premium_rate)` where `premium_rate` is
  per-source in `data/auction_houses.py`.
- `price_per_m2_usd` = `price_with_premium_usd / area_m2`.  Sculptures /
  prints / drawings / medals get `NULL` $/m² (different markets).

## Cross-source dedup

Le Auction (Vietnamese auction house) gets re-listed on Invaluable as
"Cadmore Auctions".  When the same lot appears in both, the dedup script in
the conversation history kept the Invaluable copy (Cadmore display) and
deleted the LE Auction native row, because Invaluable has wider visibility.

Match criteria: same `artist_id`, same `sale_date`, dimensions match either
exactly OR after W↔H swap.  Off-by-2cm matches were left as separate lots —
they could be different paintings.

## Stats recompute

After any bulk data change (dedup, swap, unmap, title-clean), run the
`auction_count` / `overall_*` recompute on the artists table using only the
displayed-filter sales (sold + price_usd > 0 + sale_date <= today).  Otherwise
`auction_count` drifts away from what the UI actually shows.

**Auction-less artists (gallery / exhibition pricing).**  Some VN artists
(Trần Văn Thảo, Tào Linh, Bùi Văn Tuất, etc. — ~48 in current DB) have no
auction-house sales but do appear in `price_observations` (collected from
gallery price-lists and exhibition catalogs).  The recompute falls back to
`price_observations` rows where `currency='USD'` and `price_amount > 0`
when an artist has zero auction rows — so `overall_min_usd / max / avg /
median_per_m2` get populated from gallery prices instead of left null.

`auction_count` still reflects only sale_results, so the UI shows the count
separately from the price band.  When both auction and observation data exist,
prefer auction.

## Things NOT to do (lessons paid for)

- Don't auto-`git add -A`.  The `public/Triển lãm/` folder once leaked private
  PDFs into a public repo (already added to `.gitignore`).
- Don't `scp .env.local` to a remote host — the classifier blocks this for
  good reason.  Run discovery on the remote, dump non-secret state to stdout,
  and patch from local with the env file there.
- Don't truncate French artist names at the hyphen.  See the JEAN bug.
- Don't aggressively fuzzy-match short `artist_name_raw` strings ("Jean",
  "Le Pho") to the existing artist set — short strings collide.  Require at
  least a token match between raw and the artist's normalized name.
- Don't push back from a one-off SQL pass to "fix everything everywhere"
  without first confirming the per-source convention.  The W↔H swap had to
  EXCLUDE Christie's JSON-handled lots and Le Auction native lots because
  those parsers already assign columns correctly.

## Index of one-off scripts in conversation history

These ran exactly once and aren't checked in.  If you need them again, search
the session transcript or rewrite from the convention above:

- `Aguttes refetch via meta description` — extracts artwork title from
  `<meta name="description">` after stripping `ARTIST (YYYY-YYYY)` prefix.
- `Christies dim backfill` — fetches detail pages with `urllib.request`,
  parses the JSON `"height_cm":"X","width_cm":"Y"` field.
- `Sotheby's dim backfill` — fetches detail pages, matches
  `>(\d+(?:\.\d+)?)\s+by\s+(\d+(?:\.\d+)?)\s*cm` (anchored at `>` to avoid
  matching `Executed in 1941.40 by …`).
- `LE Auction URL portalKey backfill` — calls Bidspirit
  `loadAuctionDayCatalog` per (auctionId, dayId), reads `portalKey`, rewrites
  `source_url`.
- `Cross-source dedup` — finds (artist_id, sale_date) pairs that appear in
  both `le_auction` and `invaluable` sources, matches by sorted (w, h),
  deletes LE Auction row, copies VN title onto Invaluable row.
- `Width/height swap for H × W convention sources` — for each source in
  `_HW_FIRST_SOURCES`, swaps `width_cm` and `height_cm`, rebuilds the
  `dimensions` string to match canonical W × H storage.
