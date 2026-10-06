from app import summary as sm
from app.risk import AreaOpinion, Finding, Level


def area(name, level, findings=(), recs=(), nc=()):
    return AreaOpinion(area=name, level=level, summary=f"{name} 요지입니다.", findings=list(findings), recommendations=list(recs), not_checked=list(nc))


def test_overall_follows_worst_area_and_names_drivers():
    o = sm.summarize([area("라이선스", Level.low), area("평판(제재)", Level.high), area("기타", Level.high)])
    assert o.level == Level.high and o.drivers == ["평판(제재)", "기타"]
    assert "권고하지 않습니다" in o.verdict and "평판(제재), 기타" in o.verdict


def test_low_overall_still_lists_caveats_and_never_claims_safe():
    o = sm.summarize([area("라이선스", Level.low, nc=["샘플 PII 탐지"])])
    assert o.level == Level.low and o.drivers == []
    assert "샘플 PII 탐지" in o.caveats and any("법률 자문이 아닌" in c for c in o.caveats)


def test_unknown_is_not_treated_as_low():
    o = sm.summarize([area("라이선스", Level.low), area("개인정보", Level.unknown)])
    assert o.level == Level.unknown and "사용 가능하다고 보지 마세요" in o.verdict


def test_points_are_worst_first_capped_with_evidence_and_skip_low():
    fs = [Finding(check="a", level=Level.low, note="문제 없다."),
          Finding(check="b", level=Level.medium, note="첫 문장이다. 둘째 문장이다.", evidence_url="https://e/b"),
          Finding(check="c", level=Level.high, note="높은 쟁점이다."),
          Finding(check="d", level=Level.unknown, note="확인 필요하다."),
          Finding(check="e", level=Level.medium, note="네 번째다.")]
    pts = sm.summarize([area("x", Level.high, fs)]).areas[0].points
    assert [p.check for p in pts] == ["c", "b", "e"]  # 낮음은 빼고 높음→중간 순, 최대 3개
    assert pts[1].text == "첫 문장이다." and pts[1].evidence_url == "https://e/b"


def test_priorities_ordered_by_area_level_deduped_and_capped():
    o = sm.summarize([area("라이선스", Level.medium, recs=["라이선스 원문 확인"]), area("평판(제재)", Level.high, recs=["동일 대상 확인", "동일 대상 확인"]),
                      area("기타", Level.low, recs=["무시됨"])])
    assert o.priorities == ["[평판(제재)] 동일 대상 확인", "[라이선스] 라이선스 원문 확인"]


def test_analyze_stores_opinion_and_old_report_gets_one_on_read():
    from app.models import DataCard, DatasetRef, Platform
    from app import risk
    card = DataCard(id="opinion-card", dataset=DatasetRef(platform=Platform.huggingface, repo="a/b"))
    rep = risk.analyze(card)
    assert rep.opinion and rep.opinion["level"] == rep.overall.value
    assert [a["area"] for a in rep.opinion["areas"]] == [a.area for a in rep.areas]


def test_point_repeating_the_area_headline_is_dropped():
    a = AreaOpinion(area="x", level=Level.unknown, summary="포함 여부를 확인하지 못했다. 추가 설명.",
                    findings=[Finding(check="a", level=Level.unknown, note="포함 여부를 확인하지 못했다."), Finding(check="b", level=Level.unknown, note="다른 쟁점이다.")])
    assert [p.check for p in sm.summarize([a]).areas[0].points] == ["b"]
