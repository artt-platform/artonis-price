-- Limit the synthetic-hammer trigger to source=invaluable.
--
-- The original guard (20260627000000) and its relaxation
-- (20260627010000) block any row where hammer = midpoint × 1.1 of
-- the estimate range, regardless of source.  That fingerprint was
-- correct ONLY for the Invaluable midpoint-synth bug em fixed
-- 2026-06-27: every fake row had a hammer landing exactly at
-- midpoint × 1.1.
--
-- Operator 2026-06-28: Millon re-crawl produced 4 batches of REAL
-- auction outcomes (1100, 550, 5500, 1100 EUR) that coincidentally
-- match midpoint × 1.1 of their estimate ranges because French
-- auctions land at round bid increments (€500, €1000, €5000) that
-- happen to be midpoint × 1.1 for round-number estimates
-- (€400-600, €800-1200, €4000-6000, etc.).  Trigger was rejecting
-- these legitimate hammers and the sync silently dropped 227 of
-- 878 rows.
--
-- Limiting the guard to source=invaluable preserves the protection
-- where it matters (the crawler that historically had the bug) and
-- stops blocking legitimate Millon / Drouot / Bonhams / Christies
-- outcomes that happen to round to midpoint × 1.1.

create or replace function _guard_midpoint_hammer() returns trigger as $$
declare
  mp numeric;
begin
  -- Only enforce on Invaluable rows.  Every other source's hammer
  -- comes from a primary catalog scrape (Adjugé / Résultat / Sold
  -- for / soldAmount field) and round-number coincidences with
  -- midpoint × 1.1 are real auction outcomes.
  if new.source = 'invaluable'
     and new.hammer_price is not null
     and new.estimate_low is not null
     and new.estimate_high is not null
     and new.estimate_high > 0 then
    mp := (new.estimate_low + new.estimate_high) / 2.0;
    if abs(new.hammer_price - mp * 1.10) < 1 then
      raise exception
        'synthetic hammer (midpoint × 1.1) rejected — '
        'use real Sold price or leave hammer null + status=estimate_only. '
        'lot %, hammer=%, est=[% , %]',
        new.id, new.hammer_price, new.estimate_low, new.estimate_high;
    end if;
  end if;
  return new;
end;
$$ language plpgsql;
