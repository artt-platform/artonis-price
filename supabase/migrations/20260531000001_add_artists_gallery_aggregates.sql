-- Add gallery-price aggregates to artists (computed from price_observations).
-- These complement overall_*_usd which are auction-only.
alter table artists
  add column min_price numeric(14,2),
  add column max_price numeric(14,2),
  add column avg_price numeric(14,2);
