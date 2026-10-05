"""데이터 카드 저장소. DATABASE_URL(Supabase Postgres)이 있으면 odr 스키마, 없으면 메모리."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import DataCard
from .lineage import LineageMap
from .risk import RiskReport


class Store(Protocol):
    async def save(self, card: DataCard, created_by: str | None = None) -> DataCard: ...
    async def get(self, card_id: str) -> DataCard | None: ...
    async def update(self, card: DataCard) -> DataCard: ...
    async def recent(self, limit: int = 20) -> list[dict]: ...
    async def save_risk(self, report: RiskReport) -> RiskReport: ...
    async def latest_risk(self, card_id: str) -> RiskReport | None: ...
    async def save_lineage(self, m: LineageMap) -> LineageMap: ...
    async def latest_lineage(self, card_id: str) -> LineageMap | None: ...


class MemoryStore:
    def __init__(self) -> None:
        self._cards: dict[str, DataCard] = {}
        self._risks: dict[str, RiskReport] = {}
        self._maps: dict[str, LineageMap] = {}

    async def save(self, card: DataCard, created_by: str | None = None) -> DataCard:
        card = card.model_copy(update={"id": card.id or str(uuid.uuid4())})
        self._cards[card.id] = card
        return card

    async def get(self, card_id: str) -> DataCard | None:
        return self._cards.get(card_id)

    async def update(self, card: DataCard) -> DataCard:
        self._cards[card.id] = card
        return card

    async def recent(self, limit: int = 20) -> list[dict]:
        cards = sorted(self._cards.values(), key=lambda c: c.created_at, reverse=True)[:limit]
        return [_summary(c) for c in cards]

    async def save_risk(self, report: RiskReport) -> RiskReport:
        report = report.model_copy(update={"id": report.id or str(uuid.uuid4())})
        self._risks[report.datacard_id] = report
        return report

    async def latest_risk(self, card_id: str) -> RiskReport | None:
        return self._risks.get(card_id)

    async def save_lineage(self, m: LineageMap) -> LineageMap:
        m = m.model_copy(update={"id": m.id or str(uuid.uuid4())})
        self._maps[m.datacard_id] = m
        return m

    async def latest_lineage(self, card_id: str) -> LineageMap | None:
        return self._maps.get(card_id)


def _summary(card: DataCard) -> dict:
    name = card.fields["name"].value
    return {
        "id": card.id,
        "dataset_uid": card.dataset.uid,
        "name": name.get("official") if isinstance(name, dict) else name,
        "created_at": card.created_at.isoformat(),
    }


def _card_json(card: DataCard) -> str:
    # 스냅샷 본문은 별도 테이블에 두고 카드에는 URL·시각만 남긴다
    data = card.model_dump(mode="json")
    data["sources"] = [{k: v for k, v in s.items() if k != "content"} for s in data["sources"]]
    return json.dumps(data, ensure_ascii=False)


def clean_dsn(dsn: str) -> str:
    """Supabase 연결 문자열의 `pgbouncer=true`는 libpq가 모르는 옵션이라 제거한다."""
    parts = urlsplit(dsn.strip().strip("\"'"))
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "pgbouncer"]
    return urlunsplit(parts._replace(query=urlencode(query)))


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = clean_dsn(dsn)

    async def _conn(self):
        import psycopg

        return await psycopg.AsyncConnection.connect(self.dsn, autocommit=False, prepare_threshold=None)

    async def save(self, card: DataCard, created_by: str | None = None) -> DataCard:
        card = card.model_copy(update={"id": card.id or str(uuid.uuid4())})
        ref = card.dataset
        async with await self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                """insert into odr.datasets (uid, platform, repo, version, revision, url)
                   values (%s, %s, %s, %s, %s, %s) on conflict (uid) do nothing""",
                (ref.uid, ref.platform.value, ref.repo, ref.version, ref.revision, ref.url),
            )
            await cur.execute(
                "insert into odr.datacards (id, dataset_uid, card, created_by) values (%s, %s, %s::jsonb, %s)",
                (card.id, ref.uid, _card_json(card), created_by),
            )
            for s in card.sources:
                await cur.execute(
                    """insert into odr.source_snapshots
                       (datacard_id, url, fetched_at, content_type, content_sha256, content)
                       values (%s, %s, %s, %s, %s, %s)""",
                    (card.id, s.url, s.fetched_at, s.content_type, hashlib.sha256(s.content.encode()).hexdigest(), s.content),
                )
            await conn.commit()
        return card

    async def get(self, card_id: str) -> DataCard | None:
        try:
            uuid.UUID(card_id)
        except ValueError:
            return None
        async with await self._conn() as conn, conn.cursor() as cur:
            await cur.execute("select card from odr.datacards where id = %s", (card_id,))
            row = await cur.fetchone()
        return DataCard.model_validate(row[0]) if row else None

    async def update(self, card: DataCard) -> DataCard:
        async with await self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "update odr.datacards set card = %s::jsonb, updated_at = %s where id = %s",
                (_card_json(card), datetime.now(timezone.utc), card.id),
            )
            await conn.commit()
        return card

    async def recent(self, limit: int = 20) -> list[dict]:
        async with await self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                """select id, dataset_uid, card->'fields'->'name'->'value'->>'official', created_at
                   from odr.datacards order by created_at desc limit %s""",
                (limit,),
            )
            rows = await cur.fetchall()
        return [{"id": str(r[0]), "dataset_uid": r[1], "name": r[2], "created_at": r[3].isoformat()} for r in rows]


    async def save_risk(self, report: RiskReport) -> RiskReport:
        report = report.model_copy(update={"id": report.id or str(uuid.uuid4())})
        async with await self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "insert into odr.risk_reports (id, datacard_id, report) values (%s, %s, %s::jsonb)",
                (report.id, report.datacard_id, report.model_dump_json()),
            )
            await conn.commit()
        return report

    async def latest_risk(self, card_id: str) -> RiskReport | None:
        try:
            uuid.UUID(card_id)
        except ValueError:
            return None
        async with await self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "select report from odr.risk_reports where datacard_id = %s order by created_at desc limit 1", (card_id,)
            )
            row = await cur.fetchone()
        return RiskReport.model_validate(row[0]) if row else None


    async def save_lineage(self, m: LineageMap) -> LineageMap:
        m = m.model_copy(update={"id": m.id or str(uuid.uuid4())})
        async with await self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "insert into odr.lineage_maps (id, datacard_id, map) values (%s, %s, %s::jsonb)",
                (m.id, m.datacard_id, m.model_dump_json()),
            )
            await conn.commit()
        return m

    async def latest_lineage(self, card_id: str) -> LineageMap | None:
        try:
            uuid.UUID(card_id)
        except ValueError:
            return None
        async with await self._conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "select map from odr.lineage_maps where datacard_id = %s order by created_at desc limit 1", (card_id,)
            )
            row = await cur.fetchone()
        return LineageMap.model_validate(row[0]) if row else None


def make_store(dsn: str | None) -> Store:
    return PostgresStore(dsn) if dsn else MemoryStore()
