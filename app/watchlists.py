"""제재·감시 목록 적재와 이름 대조 (기획안 7-3장).

무료 공개 자료만 쓴다: 미국 통합 제재 목록(CSL), EU 통합 제재 목록, 그리고 법무가 직접 정리하는 수동 목록
(`data/watchlists/*.csv`). 목록을 받지 못하면 '일치 없음'이 아니라 '대조하지 못함'으로 보고한다.
이름 일치는 동명이인 오탐이 많아, 호출자는 일치 근거를 보여 주고 사람이 확인하게 해야 한다.
"""
from __future__ import annotations

import asyncio
import csv
import difflib
import io
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import httpx

US_CSL_URL = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.csv"
EU_FSF_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"
MANUAL_DIR = Path(__file__).resolve().parent.parent / "data" / "watchlists"
TTL_SECONDS = 24 * 3600

_SUFFIXES = {
    "inc", "incorporated", "ltd", "limited", "llc", "llp", "corp", "corporation", "co", "company", "plc",
    "gmbh", "ag", "sa", "bv", "nv", "oy", "ab", "as", "the", "of", "pjsc", "ojsc", "jsc", "ooo", "zao",
    "주식회사", "유한회사", "유", "주",
}
csv.field_size_limit(10_000_000)


@dataclass
class Entry:
    list_id: str
    list_name: str
    jurisdiction: str
    name: str
    aliases: list[str] = field(default_factory=list)
    program: str = ""
    source_url: str = ""
    detail: str = ""


@dataclass
class Match:
    entry: Entry
    kind: str  # "exact" | "similar"
    score: float
    matched_name: str


@dataclass
class ListStatus:
    list_id: str
    list_name: str
    jurisdiction: str
    ok: bool
    count: int = 0
    error: str = ""
    fetched_at: float = 0.0


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).casefold()
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = [t for t in s.split() if t not in _SUFFIXES]
    return " ".join(tokens)


class Index:
    def __init__(self, entries: list[Entry], statuses: list[ListStatus]) -> None:
        self.entries = entries
        self.statuses = statuses
        self._exact: dict[str, list[tuple[Entry, str]]] = {}
        self._tokens: dict[str, list[tuple[Entry, str, str]]] = {}
        for e in entries:
            for n in [e.name, *e.aliases]:
                key = norm(n)
                if not key:
                    continue
                self._exact.setdefault(key, []).append((e, n))
                toks = key.split()
                if toks:
                    self._tokens.setdefault(max(toks, key=len), []).append((e, n, key))

    @property
    def loaded(self) -> list[ListStatus]:
        return [s for s in self.statuses if s.ok]

    def match(self, name: str, limit: int = 5) -> list[Match]:
        key = norm(name)
        if not key:
            return []
        found: dict[int, Match] = {}
        for e, n in self._exact.get(key, []):
            found.setdefault(id(e), Match(e, "exact", 1.0, n))
        # 너무 짧은 이름은 유사 일치를 하지 않는다 (오탐 방지)
        if len(key) >= 5:
            toks = key.split()
            for tok in {t for t in toks if len(t) >= 4}:
                for e, n, k in self._tokens.get(tok, []):
                    if id(e) in found:
                        continue
                    score = difflib.SequenceMatcher(None, key, k).ratio()
                    if score >= 0.88:
                        found[id(e)] = Match(e, "similar", round(score, 2), n)
        ranked = sorted(found.values(), key=lambda m: (m.kind != "exact", -m.score))
        return ranked[:limit]


# --- 파서 ----------------------------------------------------------------------

def parse_csl(text: str) -> list[Entry]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        source = (row.get("source") or "미국 통합 제재 목록").strip()
        out.append(Entry(
            list_id="us_csl", list_name=source, jurisdiction="미국", name=name,
            aliases=[a.strip() for a in (row.get("alt_names") or "").split(";") if a.strip()],
            program=(row.get("programs") or "").strip(),
            source_url=(row.get("source_list_url") or row.get("source_information_url") or US_CSL_URL).strip(),
            detail=" / ".join(x for x in [(row.get("addresses") or "")[:160], (row.get("remarks") or "")[:160]] if x),
        ))
    return out


def parse_eu(text: str) -> list[Entry]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";")
    cols = reader.fieldnames or []
    name_col = next((c for c in cols if c.endswith("NameAlias_WholeName")), None)
    id_col = next((c for c in cols if c.endswith("Entity_LogicalId")), None)
    if not name_col or not id_col:
        raise ValueError("EU 목록의 열 구성이 예상과 다릅니다")
    prog_col = next((c for c in cols if "Programme" in c), None)
    by_id: dict[str, Entry] = {}
    for row in reader:
        name = (row.get(name_col) or "").strip().strip('"')
        eid = (row.get(id_col) or "").strip()
        if not name or not eid:
            continue
        e = by_id.get(eid)
        if e is None:
            by_id[eid] = Entry("eu_fsf", "EU 통합 제재 목록", "EU", name, program=(row.get(prog_col) or "").strip() if prog_col else "", source_url="https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions")
        elif name != e.name and name not in e.aliases:
            e.aliases.append(name)
    return list(by_id.values())


def load_manual(directory: Path = MANUAL_DIR) -> tuple[list[Entry], int]:
    entries: list[Entry] = []
    files = sorted(directory.glob("*.csv")) if directory.exists() else []
    for f in files:
        for row in csv.DictReader(f.open(encoding="utf-8-sig")):
            name = (row.get("name") or "").strip()
            if not name or not (row.get("source_url") or "").strip():
                continue  # 근거 URL이 없는 항목은 쓰지 않는다
            entries.append(Entry(
                list_id="manual:" + (row.get("list") or f.stem).strip(), list_name=(row.get("list") or f.stem).strip(),
                jurisdiction=(row.get("jurisdiction") or "기타").strip(), name=name,
                aliases=[a.strip() for a in (row.get("aliases") or "").split(";") if a.strip()],
                program=(row.get("program") or "").strip(), source_url=row["source_url"].strip(), detail=(row.get("note") or "").strip(),
            ))
    return entries, len(files)


# --- 적재와 캐시 ----------------------------------------------------------------

async def _fetch(client: httpx.AsyncClient, url: str, parser, list_id: str, list_name: str, jurisdiction: str):
    try:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        entries = parser(r.text)
        if not entries:
            raise ValueError("파싱된 항목이 없습니다")
        return entries, ListStatus(list_id, list_name, jurisdiction, True, len(entries), fetched_at=time.time())
    except Exception as e:  # 한 목록의 실패가 전체 분석을 막지 않는다
        return [], ListStatus(list_id, list_name, jurisdiction, False, error=f"{type(e).__name__}: {e}"[:200], fetched_at=time.time())


async def build_index(client: httpx.AsyncClient, *, us_url: str = US_CSL_URL, eu_url: str = EU_FSF_URL, manual_dir: Path = MANUAL_DIR) -> Index:
    (us, us_s), (eu, eu_s) = await asyncio.gather(
        _fetch(client, us_url, parse_csl, "us_csl", "미국 통합 제재 목록(CSL)", "미국"),
        _fetch(client, eu_url, parse_eu, "eu_fsf", "EU 통합 제재 목록", "EU"),
    )
    manual, n_files = load_manual(manual_dir)
    statuses = [us_s, eu_s]
    names = sorted({e.list_name for e in manual})
    if manual:
        for n in names:
            es = [e for e in manual if e.list_name == n]
            statuses.append(ListStatus(es[0].list_id, n + " (수동)", es[0].jurisdiction, True, len(es), fetched_at=time.time()))
    return Index(us + eu + manual, statuses)


class IndexCache:
    def __init__(self, loader) -> None:
        self._loader = loader
        self._index: Index | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> Index:
        async with self._lock:
            ok = self._index is not None and any(s.ok for s in self._index.statuses)
            # 성공했으면 하루, 전부 실패했으면 10분 뒤 다시 시도한다
            ttl = TTL_SECONDS if ok else 600
            if self._index is None or time.time() - self._at > ttl:
                async with httpx.AsyncClient(timeout=90) as client:
                    self._index = await self._loader(client)
                self._at = time.time()
            return self._index
