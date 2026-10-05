from types import SimpleNamespace

import pytest

from app import lineage as ln
from app.models import CardField, DataCard, DatasetRef, Platform
from app.risk import AreaOpinion, Level


def card(repo="org/root", **fields) -> DataCard:
    c = DataCard(id=repo, dataset=DatasetRef(platform=Platform.huggingface, repo=repo, url=f"https://huggingface.co/datasets/{repo}"))
    for k, v in fields.items():
        c.fields[k] = CardField.confirmed(v, "https://huggingface.co/x")
    return c


def area(name, level):
    return AreaOpinion(area=name, level=level, summary="", findings=[])


class Env:
    """상위 데이터셋 그래프를 흉내 낸다: repo → (카드, 영역 등급, 원본 목록)."""

    def __init__(self, graph, broken=()):
        self.graph, self.broken, self.built = graph, set(broken), []

    async def build_child(self, ref):
        self.built.append(ref.repo)
        if ref.repo in self.broken:
            raise RuntimeError("403")
        return card(ref.repo)

    async def areas_for(self, c):
        lic, pii = self.graph[c.dataset.repo][0]
        return [area("라이선스", lic), area("개인정보", pii)]

    async def upstreams_for(self, c):
        return self.graph.get(c.dataset.repo, (None, []))[1]


def ds(name):
    return ln.Upstream(name=name, kind="데이터셋", url=f"https://huggingface.co/datasets/{name}")


async def run(env, root="org/root", depth=1):
    return await ln.trace(card(root), depth=depth, areas_for=env.areas_for, build_child=env.build_child, upstreams_for=env.upstreams_for)


async def test_upstream_dataset_risk_propagates_up():
    env = Env({"org/root": ((Level.low, Level.low), [ds("a/noncommercial")]), "a/noncommercial": ((Level.high, Level.low), [])})
    area_, nodes = await run(env)
    assert area_.level == Level.high
    assert nodes[0].name == "a/noncommercial" and nodes[0].level == Level.high and nodes[0].area_levels["라이선스"] == Level.high
    assert "깊이 제한" in nodes[0].summary  # depth=1이면 그 위는 추적하지 않는다


async def test_depth_two_follows_grandparents_and_bubbles_up():
    env = Env({
        "org/root": ((Level.low, Level.low), [ds("a/mid")]),
        "a/mid": ((Level.low, Level.low), [ds("b/leaf")]),
        "b/leaf": ((Level.medium, Level.low), [ln.Upstream(name="Common Crawl", kind="웹 크롤링")]),
    })
    area_, nodes = await run(env, depth=2)
    by = {n.name: n for n in nodes}
    assert by["b/leaf"].parent_id == by["a/mid"].id and by["b/leaf"].depth == 2
    assert by["a/mid"].level == Level.medium and area_.level == Level.medium
    assert env.built == ["a/mid", "b/leaf"]


async def test_missing_upstreams_is_a_lineage_break_medium():
    area_, nodes = await run(Env({"org/root": ((Level.low, Level.low), [])}))
    assert area_.level == Level.medium and nodes == []
    assert area_.findings[0].check == "원본 출처 공개 여부"


async def test_non_dataset_kinds_use_rules():
    env = Env({"org/root": ((Level.low, Level.low), [
        ln.Upstream(name="Common Crawl", kind="웹 크롤링"),
        ln.Upstream(name="CCTV 녹음", kind="직접 수집"),
    ])})
    area_, nodes = await run(env)
    assert {n.name: n.level for n in nodes} == {"Common Crawl": Level.medium, "CCTV 녹음": Level.unknown}
    assert area_.level == Level.medium


async def test_inaccessible_or_unresolvable_upstream_is_unknown_node_medium():
    env = Env({"org/root": ((Level.low, Level.low), [ds("a/private"), ln.Upstream(name="어떤 데이터셋", kind="데이터셋")])}, broken={"a/private"})
    area_, nodes = await run(env)
    assert [n.level for n in nodes] == [Level.medium, Level.medium]
    assert "확인 불가" in nodes[0].summary and "RuntimeError" in nodes[0].summary


async def test_cycles_and_node_cap():
    env = Env({"org/root": ((Level.low, Level.low), [ds("org/root"), ds("a/x")]), "a/x": ((Level.low, Level.low), [])})
    _, nodes = await run(env)
    assert "순환" in nodes[0].summary and env.built == ["a/x"]  # 자기 자신은 다시 받지 않는다

    many = [ln.Upstream(name=f"site{i}", kind="웹 크롤링") for i in range(20)]
    area_, nodes = await run(Env({"org/root": ((Level.low, Level.low), many)}))
    assert len(nodes) == ln.MAX_NODES and any(f.check == "추적 생략" for f in area_.findings)


def test_baseline_upstreams_parse_card_field():
    c = card(upstream_sources=["allenai/c4", "https://www.kaggle.com/datasets/u/x", "Some Corpus", "original"])
    got = {u.name: u for u in ln.baseline_upstreams(c)}
    assert got["allenai/c4"].kind == "데이터셋" and got["allenai/c4"].url.endswith("datasets/allenai/c4")
    assert ln.resolve_ref(got["allenai/c4"]).repo == "allenai/c4"
    assert got["Some Corpus"].kind == "기타" and "original" not in got
    kaggle = next(u for u in got.values() if "kaggle" in (u.url or ""))
    assert ln.resolve_ref(kaggle).platform == Platform.kaggle
    assert ln.baseline_upstreams(card(upstream_sources={"source_datasets": ["original"]})) == []


def test_llm_upstreams_drop_names_not_in_documents():
    c = card(creation="Built from Common Crawl snapshots.")
    parsed = ln._Upstreams(upstreams=[
        ln._Extracted(name="Common Crawl", kind="웹 크롤링", url="https://invented.example", evidence_url="https://x", evidence_quote="Built from Common Crawl"),
        ln._Extracted(name="Imaginary Corpus", kind="데이터셋", url=None, evidence_url="https://x", evidence_quote="-"),
    ])
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: SimpleNamespace(parsed_output=parsed))))
    got = ln.llm_upstreams(c, client)
    assert [u.name for u in got] == ["Common Crawl"] and got[0].url is None  # 문서에 없는 URL도 버린다
