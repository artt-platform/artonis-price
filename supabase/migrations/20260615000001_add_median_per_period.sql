-- Period-bucketed median $/m² view. Compares each lot's $/m² to the median
-- of contemporaneous lots from the same artist+support, in 5-year buckets:
--   2005-2009, 2010-2014, 2015-2019, 2020-2024, 2025-2029.
--
-- Why: VN art market appreciated 10-50× between 2010 and 2025. Comparing
-- a 2012 Lê Phổ painting against the all-time median (dominated by recent
-- $100K+ sales) flagged hundreds of legitimate early-market sales as
-- "suspiciously low". A 2012 sale at $7K is normal for 2012, not suspicious.

create or replace view artist_median_per_support_period as
with src as (
  select artist_id, support_type,
         (extract(year from sale_date)::int / 5) * 5 as period_start,
         price_per_m2_usd
  from sale_results
  where kind = 'painting'
    and price_per_m2_usd > 0
    and price_per_m2_usd < 50000000
    and support_type is not null
    and artist_id is not null
    and sale_date is not null
), ranked as (
  select artist_id, support_type, period_start, price_per_m2_usd,
         row_number() over (
           partition by artist_id, support_type, period_start
           order by price_per_m2_usd) as rn,
         count(*) over (
           partition by artist_id, support_type, period_start) as cnt
  from src
)
select artist_id, support_type, period_start, cnt as n,
       avg(case when rn = (cnt+1)/2 or rn = cnt/2+1 then price_per_m2_usd end) as median_ppm
from ranked
where cnt >= 3
group by artist_id, support_type, period_start, cnt;
