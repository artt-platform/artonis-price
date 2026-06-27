-- Relax the midpoint-hammer guard.
--
-- The original trigger (20260627000000_midpoint_hammer_trigger.sql)
-- blocked both:
--   1. hammer = exact midpoint of estimate
--   2. hammer = midpoint × 1.1
--
-- The ORIGINAL synthesised-hammer bug was rule (2): every fake lot
-- the Invaluable crawler generated landed at midpoint × 1.1.  Rule
-- (1) was a defensive add-on.
--
-- Operator 2026-06-27 caught a real Sothebys / Invaluable Sold price
-- being rejected (Bui Huu Hung 'Green Bamboo' on Material Culture
-- via Invaluable, estimate $1,500-$2,500, real soldAmount $2,000 =
-- exact midpoint) because the trigger flagged the coincidence.
--
-- Real auctions DO land at exact midpoint sometimes — round-number
-- bids on round-number estimates.  Keep the × 1.1 block (which is
-- the actual synthesis fingerprint, position 0.55-0.85 of range)
-- and drop the exact-midpoint block.

create or replace function _guard_midpoint_hammer() returns trigger as $$
declare
  mp numeric;
begin
  if new.hammer_price is not null
     and new.estimate_low is not null
     and new.estimate_high is not null
     and new.estimate_high > 0 then
    mp := (new.estimate_low + new.estimate_high) / 2.0;
    -- Only block midpoint × 1.1 (the synth fingerprint).  Exact
    -- midpoint is a legitimate auction outcome — keep the row.
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
