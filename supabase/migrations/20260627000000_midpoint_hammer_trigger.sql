-- Block any hammer_price that matches midpoint × 1.1 of estimate range.
-- Third recurrence of the synthetic-hammer bug 2026-06-27 — this trigger
-- makes a future regression visible at INSERT/UPDATE time instead of
-- silently corrupting price aggregates.
create or replace function _guard_midpoint_hammer() returns trigger as $$
declare
  mp numeric;
begin
  if new.hammer_price is not null
     and new.estimate_low is not null
     and new.estimate_high is not null
     and new.estimate_high > 0 then
    mp := (new.estimate_low + new.estimate_high) / 2.0;
    if abs(new.hammer_price - mp) < 0.01
       or abs(new.hammer_price - mp * 1.10) < 1 then
      raise exception
        'synthetic hammer (midpoint × 1.1 or exact midpoint) rejected — '
        'use real Sold price or leave hammer null + status=estimate_only. '
        'lot %, hammer=%, est=[% , %]',
        new.id, new.hammer_price, new.estimate_low, new.estimate_high;
    end if;
  end if;
  return new;
end;
$$ language plpgsql;

drop trigger if exists guard_midpoint_hammer on sale_results;
create trigger guard_midpoint_hammer
  before insert or update of hammer_price, estimate_low, estimate_high
  on sale_results
  for each row execute function _guard_midpoint_hammer();
