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
    """상위 데이터셋 그래프를 흉내 낸다: repo → ((라이선스, 개인정보) 등급, 원본 목록)."""

    def __init__(self, graph, broken=()):
        self.graph, self.broken = graph, set(broken)
        self.built, self.analyzed = [], []

    async def build_child(self, ref):
        self.built.append(ref.repo)
        if ref.repo in self.broken:
            raise RuntimeError("403")
        c = card(ref.repo)
        c.fields["description"] = CardField.confirmed({"original": f"# {ref.repo}\n\n**{ref.repo}** 데이터셋 설명입니다."}, "https://x")
        c.fields["provider"] = CardField.confirmed({"name": ref.repo.split("/")[0]}, "https://x")
        c.fields["license"] = CardField.confirmed({"raw": ["cc-by-4.0"], "spdx": ["CC-BY-4.0"], "custom": []}, "https://x")
        return c

    async def areas_for(self, c):
        self.analyzed.append(c.dataset.repo)
        lic, pii = self.graph[c.dataset.repo][0]
        return [area("라이선스", lic), area("개인정보", pii)]

    async def upstreams_for(self, c):
        return self.graph.get(c.dataset.repo, (None, []))[1]


def ds(name):
    return ln.Upstream(name=name, kind="데이터셋", url=f"https://huggingface.co/datasets/{name}")


L = Level.low
CHAIN = {
    "org/root": ((L, L), [ds("a/mid"), ln.Upstream(name="Common Crawl", kind="웹 크롤링")]),
    "a/mid": ((L, L), [ds("b/leaf")]),
    "b/leaf": ((Level.high, L), []),
}


async def explore(env, root="org/root", depth=ln.MAX_DEPTH):
    return await ln.explore(card(root), max_depth=depth, build_child=env.build_child, upstreams_for=env.upstreams_for)


async def analyze(env, m, selected):
    return await ln.analyze(m, selected, areas_for=env.areas_for, build_child=env.build_child)


async def test_explore_reaches_final_origin_with_basic_info_and_no_analysis():
    env = Env(CHAIN)
    m = await explore(env)
    by = {n.name: n for n in m.nodes}
    assert m.max_depth == 3 or m.max_depth == 2
    leaf = next(n for n in m.nodes if n.ref and n.ref.repo == "b/leaf")
    assert leaf.parent_id == by["a/mid"].id and leaf.depth == 2 and leaf.lineage_break  # 더 위 원본을 못 찾음
    assert "데이터셋 설명입니다" in leaf.description and "#" not in leaf.description and "**" not in leaf.description
    assert leaf.provider == "b" and leaf.license == "cc-by-4.0" and leaf.url.endswith("datasets/b/leaf")
    assert by["Common Crawl"].kind == "웹 크롤링" and by["Common Crawl"].ref is None
    assert env.analyzed == []  # 탐색은 리스크 분석을 하지 않는다


async def test_analyze_only_selected_and_propagates_from_deep_node():
    env = Env(CHAIN)
    m = await explore(env)
    leaf_id = next(n.id for n in m.nodes if n.ref and n.ref.repo == "b/leaf")
    env.built.clear()
    area_, nodes = await analyze(env, m, {leaf_id})  # 가장 깊은 원본만 고른다
    by = {n.name: n for n in nodes}
    assert env.analyzed == ["b/leaf"] and env.built == ["b/leaf"]
    assert by["b/leaf"].analyzed and by["b/leaf"].level == Level.high
    assert by["a/mid"].level == Level.high and not by["a/mid"].analyzed  # 위로 전파된다
    assert area_.level == Level.high


async def test_unselected_datasets_are_unknown_not_low():
    env = Env({"org/root": ((L, L), [ds("a/clean")]), "a/clean": ((L, L), [ln.Upstream(name="CCTV 녹음", kind="직접 수집")])})
    m = await explore(env)
    area_, nodes = await analyze(env, m, set())
    assert area_.level == Level.unknown and any(f.check == "분석하지 않은 원본" for f in area_.findings)
    assert env.analyzed == []
    _, nodes = await analyze(env, m, {n.id for n in m.nodes if n.kind == "데이터셋"})
    assert env.analyzed == ["a/clean"]


async def test_default_selection_by_depth():
    m = await explore(Env(CHAIN))
    assert {m.nodes[i].ref.repo for i in range(len(m.nodes)) if m.nodes[i].id in ln.default_selection(m, 1)} == {"a/mid"}
    assert len(ln.default_selection(m, 2)) == 2 and ln.default_selection(m, 0) == set()


async def test_root_without_upstreams_is_a_lineage_break_medium():
    env = Env({"org/root": ((L, L), [])})
    m = await explore(env)
    area_, nodes = await analyze(env, m, set())
    assert m.root_break and nodes == [] and area_.level == Level.medium
    assert area_.findings[0].check == "원본 출처 공개 여부"


async def test_non_dataset_kinds_use_rules():
    env = Env({"org/root": ((L, L), [ln.Upstream(name="Common Crawl", kind="웹 크롤링"), ln.Upstream(name="CCTV 녹음", kind="직접 수집")])})
    m = await explore(env)
    area_, nodes = await analyze(env, m, set())
    assert {n.name: n.level for n in nodes} == {"Common Crawl": Level.medium, "CCTV 녹음": Level.unknown}
    assert area_.level == Level.medium


async def test_inaccessible_or_unresolvable_upstream_is_medium():
    env = Env({"org/root": ((L, L), [ds("a/private"), ln.Upstream(name="어떤 데이터셋", kind="데이터셋")])}, broken={"a/private"})
    m = await explore(env)
    assert [n.status for n in m.nodes] == ["접근 실패", "확인 불가"]
    _, nodes = await analyze(env, m, set())
    assert [n.level for n in nodes] == [Level.medium, Level.medium] and "확인 불가" in nodes[0].summary


async def test_analysis_failure_of_one_node_does_not_break_report():
    env = Env({"org/root": ((L, L), [ds("a/x")]), "a/x": ((L, L), [])})
    m = await explore(env)
    env.broken.add("a/x")
    _, nodes = await analyze(env, m, {m.nodes[0].id})
    assert nodes[0].level == Level.medium and "실패" in nodes[0].summary


async def test_cycles_and_node_cap():
    env = Env({"org/root": ((L, L), [ds("org/root"), ds("a/x")]), "a/x": ((L, L), [])})
    m = await explore(env)
    assert m.nodes[0].status == "중복" and env.built == ["a/x"]

    many = [ln.Upstream(name=f"site{i}", kind="웹 크롤링") for i in range(30)]
    m = await explore(Env({"org/root": ((L, L), many)}))
    assert len(m.nodes) == ln.MAX_NODES + 1 and m.nodes[-1].status == "추적 생략"


async def test_depth_limit_stops_exploration():
    env = Env(CHAIN)
    m = await explore(env, depth=1)
    assert max(n.depth for n in m.nodes) == 1 and env.built == ["a/mid"]
    assert "최대 탐색 단계" in next(n for n in m.nodes if n.ref).note


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


def test_search_engine_and_flickr_get_platform_rules():
    from app.lineage import rule_for
    assert rule_for("Google image search", "플랫폼 콘텐츠")[0] == Level.high
    assert rule_for("Flickr", "플랫폼 콘텐츠")[0] == Level.medium
    assert "Flickr" in rule_for("Flickr", "플랫폼 콘텐츠")[1]
    assert rule_for("YouTube", "플랫폼 콘텐츠")[0] == Level.medium  # 규칙이 없는 플랫폼은 기본 규칙
    assert rule_for("Google's C4 dataset", "데이터셋") == rule_for("x", "데이터셋")  # 데이터셋 유형에는 적용하지 않는다
