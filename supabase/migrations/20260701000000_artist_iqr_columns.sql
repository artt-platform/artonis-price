-- Add Q1 / median / Q3 columns for artist typical-range display.
--
-- Operator 2026-07-01 — the /artists list showed min-max ranges
-- (Alix Aymé $612 → $609K, Pham Hau $1K → $1.24M) that span the
-- extremes: one-off sketches on the low end, headline masterpieces
-- on the high.  Neither number tells the operator what the artist's
-- painting actually goes for on a typical day.
--
-- Q1–Q3 (interquartile range) shows the middle 50 % — the band
-- half of realised sales land in.  Median splits it in two.  These
-- three numbers together describe the artist's real price
-- distribution far better than min-max.
--
-- refresh_artist_stats.py fills them from `sale_results` filtered
-- to status='sold' and kind='painting'.

alter table artists
  add column if not exists overall_q1_usd numeric(12,2),
  add column if not exists overall_median_usd numeric(12,2),
  add column if not exists overall_q3_usd numeric(12,2);
