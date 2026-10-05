-- 리스크 분석 결과 (라이선스·개인정보). 데이터 카드 한 건에 여러 번 재분석할 수 있다.
create table if not exists odr.risk_reports (
  id           uuid primary key default gen_random_uuid(),
  datacard_id  uuid not null references odr.datacards(id) on delete cascade,
  report       jsonb not null,
  created_at   timestamptz not null default now()
);
create index if not exists risk_reports_card_idx on odr.risk_reports (datacard_id, created_at desc);

alter table odr.risk_reports enable row level security;
revoke all on odr.risk_reports from anon, authenticated;

-- 앱 전용 롤(odr_app)이 있는 환경에서만 권한과 정책을 부여한다.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'odr_app') then
    grant select, insert, update, delete on odr.risk_reports to odr_app;
    if not exists (select 1 from pg_policies where schemaname = 'odr' and tablename = 'risk_reports') then
      create policy odr_app_risk_reports on odr.risk_reports for all to odr_app using (true) with check (true);
    end if;
  end if;
end $$;
