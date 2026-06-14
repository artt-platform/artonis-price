-- Add via_platform column to sale_results.
-- For lots sourced via an aggregator (drouot, invaluable), `source` will hold
-- the underlying auction house slug (e.g. "minerve-encheres", "saint-paul-auction")
-- and `via_platform` will hold the aggregator name ("drouot" / "invaluable").
-- For lots sourced directly from a house's own site, via_platform is null.

alter table sale_results add column if not exists via_platform text;
create index if not exists sale_results_via_platform_idx on sale_results(via_platform);

comment on column sale_results.via_platform is
  'Aggregator the lot was discovered through (drouot|invaluable). Null when crawled directly from the house.';
