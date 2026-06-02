-- Add end_date to exhibitions so we can show date range (e.g. "21/10/2025 – 23/11/2025")
alter table exhibitions
  add column end_date date;

-- Index for range queries (e.g. "exhibitions active during last 90 days")
create index exhibitions_end_date_idx on exhibitions(end_date);
