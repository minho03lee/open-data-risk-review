-- 원본 계보 지도 (분석 전에 원본별 기본 정보를 보여 주고, 분석할 원본을 고르는 데 쓴다).
create table if not exists odr.lineage_maps (
  id           uuid primary key default gen_random_uuid(),
  datacard_id  uuid not null references odr.datacards(id) on delete cascade,
  map          jsonb not null,
  created_at   timestamptz not null default now()
);
create index if not exists lineage_maps_card_idx on odr.lineage_maps (datacard_id, created_at desc);

alter table odr.lineage_maps enable row level security;
revoke all on odr.lineage_maps from anon, authenticated;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'odr_app') then
    grant select, insert, update, delete on odr.lineage_maps to odr_app;
    if not exists (select 1 from pg_policies where schemaname = 'odr' and tablename = 'lineage_maps') then
      create policy odr_app_lineage_maps on odr.lineage_maps for all to odr_app using (true) with check (true);
    end if;
  end if;
end $$;
