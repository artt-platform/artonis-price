-- Add artwork_uuid for resale detection: when the same physical artwork
-- appears at multiple auctions over time, all those sale_results rows
-- share the same artwork_uuid. Used to render a "resale timeline" so the
-- user can see appreciation rate per piece.
--
-- Population is handled by a separate clustering script
-- (supabase/cluster_resales.py) that matches on (artist, dimensions,
-- fuzzy title) and assigns a uuid per cluster of size ≥ 2.

alter table sale_results add column if not exists artwork_uuid text;
create index if not exists sale_results_artwork_uuid_idx on sale_results(artwork_uuid)
  where artwork_uuid is not null;

comment on column sale_results.artwork_uuid is
  'Shared id linking the same physical artwork across multiple sales. Null = appears in exactly one sale (or hasn''t been clustered yet).';
