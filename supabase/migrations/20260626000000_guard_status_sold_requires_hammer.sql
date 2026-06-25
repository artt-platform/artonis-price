-- 2026-06-26 — DB-level guard: status='sold' MUST have a hammer_price.
--
-- Recurring bug — second time fixed.  History:
--   1st incident (earlier): invaluable_detail_runner.py wrote
--     status='sold' for lots where Invaluable hides hammer; we
--     synthesised price_usd = midpoint(estimate) and shipped it as
--     a realized price.  Caught and patched.
--   2nd incident (2026-06-26): operator caught 4 lots in /sales
--     showing prices that don't appear on the detail page (Bui Huu
--     Hung 'Mother and Children' $5K, 3× Dao Hai Phong).  Same root
--     cause — 105 production rows with status='sold' + hammer=null +
--     synthesised price_usd from midpoint.
--
-- The crawler-level fix (eb5353b) prevents NEW rows being written
-- wrong, but doesn't catch:
--   • External backfill scripts that mutate status
--   • Manual SQL UPDATEs that bump status to 'sold'
--   • Future crawlers that copy the old midpoint pattern
--
-- DB trigger is the correct enforcement level.  Non-destructive:
-- silently coerces status='sold' + hammer_price IS NULL down to
-- 'estimate_only' on INSERT/UPDATE.  Same belt-and-suspenders pattern
-- as fill_derived_dim_fields (migration 20260625000000).

CREATE OR REPLACE FUNCTION guard_status_sold_requires_hammer()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  -- Only paintings/sculptures track hammer — let other rows pass.
  -- Status='sold' is meaningless without a hammer; coerce to the
  -- estimate-only bucket the UI already filters out.
  IF NEW.status = 'sold' AND NEW.hammer_price IS NULL THEN
    NEW.status := 'estimate_only';
    -- Also null out the fake price_usd if it was set — almost
    -- always means midpoint-of-estimate, which is misleading.
    -- (Keep estimate_low/high; those are real data.)
    IF NEW.hammer_price IS NULL THEN
      NEW.price_usd := NULL;
      NEW.price_with_premium_usd := NULL;
      NEW.price_per_m2_usd := NULL;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

COMMENT ON FUNCTION guard_status_sold_requires_hammer() IS
  'Recurring-bug guard.  Auto-demotes status=sold to estimate_only '
  'when hammer_price is NULL.  Prevents the midpoint-as-hammer '
  'pattern from leaking back in.  Belt-and-suspenders.';

DROP TRIGGER IF EXISTS sale_results_guard_status_hammer ON sale_results;

CREATE TRIGGER sale_results_guard_status_hammer
  BEFORE INSERT OR UPDATE OF status, hammer_price, price_usd
  ON sale_results
  FOR EACH ROW
  EXECUTE FUNCTION guard_status_sold_requires_hammer();

-- Backfill: any row currently in the bad state gets coerced now.
-- Should be 0 after the eb5353b backfill, but idempotent.
UPDATE sale_results
SET status = 'estimate_only',
    price_usd = NULL,
    price_with_premium_usd = NULL,
    price_per_m2_usd = NULL
WHERE status = 'sold'
  AND hammer_price IS NULL;
