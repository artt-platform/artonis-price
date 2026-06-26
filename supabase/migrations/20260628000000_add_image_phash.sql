-- Perceptual hash of the lot image so we can detect re-listed
-- artworks across auctions / houses / dates.  Operator request
-- 2026-06-28: title + dims matching produced false positives;
-- pHash on the catalog image is the right signal for duplicate
-- detection.
--
-- 16 hex chars = 64-bit pHash.  Hamming distance ≤ 10 ≈ 85%
-- similarity, the threshold used by image-search literature for
-- "same artwork allowing crop/resize/light watermark".

ALTER TABLE sale_results ADD COLUMN IF NOT EXISTS image_phash text;

-- Partial index — only lots with an image have a hash worth
-- indexing.  Equality lookups (exact match = certain duplicates)
-- go straight through; near-match still requires a function scan.
CREATE INDEX IF NOT EXISTS idx_sale_results_image_phash
  ON sale_results(image_phash) WHERE image_phash IS NOT NULL;
