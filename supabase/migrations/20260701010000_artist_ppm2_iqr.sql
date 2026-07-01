-- Add 25%-75% $/m² columns for artist detail-page per-m² band.
--
-- Operator 2026-07-01 asked for detail + list numbers to match on
-- the $/m² card too.  Previously em stored only
-- overall_median_per_m2_usd (single number) so the detail page's
-- Q1–Q3 band had to be recomputed from lot rows, which drifted from
-- whatever number the list showed.
--
-- refresh_artist_stats.py fills these from sale_results
-- (status='sold' + kind='painting' + price_per_m2_usd > 0).

alter table artists
  add column if not exists overall_q1_per_m2_usd numeric(14,2),
  add column if not exists overall_q3_per_m2_usd numeric(14,2);
