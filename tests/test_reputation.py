from types import SimpleNamespace

from app import reputation as rp
from app import watchlists as wl
from app.models import CardField, DataCard, DatasetRef, Platform, Source
from app.risk import Level
from tests.test_watchlists import CSL, EU


def card(owner="exampleorg", **fields) -> DataCard:
    c = DataCard(id="c1", dataset=DatasetRef(platform=Platform.huggingface, repo=f"{owner}/ds"))
    for k, v in fields.items():
        c.fields[k] = CardField.confirmed(v, "https://huggingface.co/x")
    return c


def idx(ok_us=True, ok_eu=True, manual=False):
    entries, statuses = [], []
    if ok_us:
        entries += wl.parse_csl(CSL)
        statuses.append(wl.ListStatus("us_csl", "미국 통합 제재 목록(CSL)", "미국", True, 2))
    else:
        statuses.append(wl.ListStatus("us_csl", "미국 통합 제재 목록(CSL)", "미국", False, error="HTTPStatusError"))
    if ok_eu:
        entries += wl.parse_eu(EU)
        statuses.append(wl.ListStatus("eu_fsf", "EU 통합 제재 목록", "EU", True, 2))
    if manual:
        entries.append(wl.Entry("manual:한국 명단", "한국 명단", "한국", "Korean Sample Org", source_url="https://example.test/kr"))
        statuses.append(wl.ListStatus("manual:한국 명단", "한국 명단 (수동)", "한국", True, 1))
    return wl.Index(entries, statuses)


def checks(area):
    return [f for f in area.findings]


def test_direct_exact_match_is_high_with_evidence():
    c = card(provider={"name": "Example Frontier Holdings"})
    a = rp.opinion(c, idx(), rp.baseline_entities(c))
    hit = next(f for f in a.findings if f.check.startswith("제재 목록 대조: Entity List"))
    assert hit.level == Level.high and hit.evidence_url == "https://example.test/el"
    assert "사람이 확인" in hit.note
    assert a.level == Level.high
    us = next(j for j in a.jurisdictions if j.jurisdiction == "미국")
    assert us.level == Level.high


def test_indirect_role_or_similar_is_medium():
    c = card()
    a = rp.opinion(c, idx(), [rp.Entity(name="Sample Sanctioned Bank", role="모회사·자금 제공자")])
    assert max(f.level for f in a.findings if f.check.startswith("제재 목록 대조:")) == Level.medium or any(
        f.level == Level.medium for f in a.findings)
    b = rp.opinion(c, idx(), [rp.Entity(name="Example Frontier Holdngs", role="제공자")])
    assert any(f.level == Level.medium and "유사" in f.note for f in b.findings)


def test_no_match_is_low_but_area_stays_unknown_without_manual_lists():
    c = card(provider={"name": "Allen Institute for AI"})
    a = rp.opinion(c, idx(), rp.baseline_entities(c))
    assert any(f.check == "제재 목록 일치" and f.level == Level.low for f in a.findings)
    assert any(f.check == "수동 목록 대조" and f.level == Level.unknown for f in a.findings)
    assert a.level == Level.unknown  # 대조하지 못한 목록이 남아 있으므로 낮음이 아니다


def test_all_lists_failed_is_unknown_not_low():
    c = card(provider={"name": "Allen Institute for AI"})
    a = rp.opinion(c, idx(ok_us=False, ok_eu=False), rp.baseline_entities(c))
    assert a.level == Level.unknown
    assert not any(f.level == Level.low for f in a.findings)
    assert all(j.level == Level.unknown for j in a.jurisdictions)


def test_manual_list_enables_low_and_korean_jurisdiction():
    c = card(provider={"name": "Allen Institute for AI"})
    a = rp.opinion(c, idx(manual=True), rp.baseline_entities(c))
    assert a.level == Level.low
    assert {j.jurisdiction: j.level for j in a.jurisdictions} == {"미국": Level.low, "EU": Level.low, "한국": Level.low}


def test_no_entities_is_unknown():
    c = card()
    c.dataset.repo = "plain"
    a = rp.opinion(c, idx(), rp.baseline_entities(c))
    assert any(f.check == "대조 대상 기관" for f in a.findings)


def test_concern_country_is_a_signal_not_a_listing():
    c = card(provider_origin="본사: 중국 베이징, 모회사 없음", provider={"name": "x"})
    a = rp.opinion(c, idx(manual=True), rp.baseline_entities(c))
    f = next(f for f in a.findings if f.check == "중국 연관")
    assert f.level == Level.unknown and "중국" in f.evidence_quote and "제재 대상이 되는 것은 아니며" in f.note


class FakeClient:
    def __init__(self, entities):
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: SimpleNamespace(
            parsed_output=rp._Entities(entities=entities), stop_reason="end_turn")))


def test_llm_entities_drop_names_not_in_documents():
    c = card(provider_origin="Example Frontier Holdings가 자금을 지원")
    c.sources.append(Source(url="https://huggingface.co/x", content="Funded by Example Frontier Holdings and Acme Labs."))
    client = FakeClient([
        rp._Extracted(name="Example Frontier Holdings", role="모회사·자금 제공자", evidence_url="https://huggingface.co/x", evidence_quote="Funded by"),
        rp._Extracted(name="Invented Corporation", role="제작자", evidence_url="https://huggingface.co/x", evidence_quote="-"),
    ])
    got = rp.llm_entities(c, client)
    assert [e.name for e in got] == ["Example Frontier Holdings"]
