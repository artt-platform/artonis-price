-- 2026-06-26 — Lot thumbnails / hero images.
--
-- Every source already serves a public image per lot (Drouot data
-- island `photo.path`, Sothebys `<img class="image-style-lot-thumbnail">`,
-- Christie's `<img class="object-image">`, Aguttes / Millon catalog HTML
-- `<img class="lot-thumb">`, etc.).  We weren't capturing them.
-- Operator: 'làm feature image đi, lấy trực tiếp từ source crawl của
-- mình'.
--
-- Stored as the full HTTPS URL.  No CDN proxy yet — Next/Image's
-- remote-patterns config in v2 handles caching + responsive sizes.

ALTER TABLE sale_results
  ADD COLUMN IF NOT EXISTS image_url text;

COMMENT ON COLUMN sale_results.image_url IS
  'Hero/thumbnail image URL for the lot, fetched from the source '
  'auction house at crawl time.  Always HTTPS.  Nullable when the '
  'source did not expose an image.';

-- No index — image_url isn't queried, only selected for display.
