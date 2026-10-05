-- 오픈데이터 리스크 검토: 기존 ai-lawsuit-monitor 테이블과 섞이지 않도록 별도 스키마(odr)를 쓴다.
create schema if not exists odr;

-- 확정된 데이터셋 (플랫폼 + 저장소 + 버전 + 커밋)
create table if not exists odr.datasets (
  uid         text primary key,
  platform    text not null check (platform in ('huggingface', 'kaggle', 'aihub', 'web')),
  repo        text not null,
  version     text,
  revision    text,
  url         text,
  created_at  timestamptz not null default now()
);

-- 데이터 카드 (항목별 값·근거·상태는 card jsonb에 저장)
create table if not exists odr.datacards (
  id           uuid primary key default gen_random_uuid(),
  dataset_uid  text not null references odr.datasets(uid),
  card         jsonb not null,
  created_by   text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists datacards_dataset_idx on odr.datacards (dataset_uid, created_at desc);

-- 분석에 쓴 페이지 스냅샷 (나중에 원문이 바뀌어도 당시 근거를 제시)
create table if not exists odr.source_snapshots (
  id            bigserial primary key,
  datacard_id   uuid not null references odr.datacards(id) on delete cascade,
  url           text not null,
  fetched_at    timestamptz not null,
  content_type  text,
  content_sha256 text not null,
  content       text not null
);
create index if not exists source_snapshots_card_idx on odr.source_snapshots (datacard_id);

-- 서버(서비스 롤)만 접근. PostgREST로 노출하지 않으며 anon/authenticated 권한 없음.
revoke all on schema odr from anon, authenticated;
