-- 2026-06-23 — Store the FULL auction-house catalog description text
-- per lot.  Until now we kept only `raw_snapshot` (≤ 500 chars) which
-- truncates the bilingual / multi-line catalog blob that carries
-- medium, year (Painted in / Executed), signature, inscription,
-- provenance, dimensions, and notes.
--
-- The LLM extraction pass (crawlers/llm_parser.py) needs the full
-- text to disambiguate, so we add a dedicated TEXT column.  Crawlers
-- populate this at insert time going forward; a backfill pass
-- re-fetches source URLs to populate it for historical rows.

ALTER TABLE sale_results
  ADD COLUMN IF NOT EXISTS catalog_description text;

COMMENT ON COLUMN sale_results.catalog_description IS
  'Full catalog description text (medium + signature + dim + provenance + notes), '
  'untruncated, in original language.  Source for crawlers/llm_parser.py.';
