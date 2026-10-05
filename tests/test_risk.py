from fastapi.testclient import TestClient

from app import main, risk
from app.models import CardField, DataCard, DatasetRef, FieldStatus, Platform
from app.risk import Level


def card(platform=Platform.huggingface, **fields) -> DataCard:
    c = DataCard(dataset=DatasetRef(platform=platform, repo="org/ds"), id="c1")
    for k, v in fields.items():
        status = FieldStatus.confirmed
        if isinstance(v, tuple):
            v, status = v
        c.fields[k] = CardField(value=v, status=status, evidence_url="https://x/readme", evidence_quote="q")
    return c


def lic(*spdx, custom=()):
    return {"raw": list(spdx) + list(custom), "spdx": list(spdx), "custom": list(custom)}


def checks(area):
    return {f.check: f for f in area.findings}


def test_missing_license_is_high_on_structured_platforms_unknown_elsewhere():
    assert risk.license_opinion(card()).level == Level.high
    assert risk.license_opinion(card(Platform.web)).level == Level.unknown


def test_noncommercial_license_is_high():
    a = risk.license_opinion(card(license=lic("CC-BY-NC-4.0")))
    assert a.level == Level.high
    assert checks(a)["상업적 이용 허용"].level == Level.high


def test_no_derivatives_is_high_and_share_alike_is_medium():
    assert checks(risk.license_opinion(card(license=lic("CC-BY-ND-4.0"))))["변경·파생 허용"].level == Level.high
    assert checks(risk.license_opinion(card(license=lic("CC-BY-SA-4.0"))))["의무 조건(SA)"].level == Level.medium


def test_permissive_license_with_known_upstream_is_not_low():
    # 원본 계보를 아직 분석하지 않았으므로 낮음으로 단정하지 않는다
    a = risk.license_opinion(card(license=lic("CC0-1.0"), upstream_sources="Common Crawl"))
    assert checks(a)["상업적 이용 허용"].level == Level.low
    assert a.level == Level.unknown
    assert any("계보" in x for x in a.not_checked)


def test_custom_license_needs_review():
    a = risk.license_opinion(card(license=lic(custom=["other"])))
    assert checks(a)["커스텀·미분류 라이선스"].level == Level.unknown


def test_llm_string_license_is_recognised():
    a = risk.license_opinion(card(license="CC-BY-NC-4.0"))
    assert a.level == Level.high


def test_inferred_license_cannot_be_low():
    a = risk.license_opinion(card(license=(lic("MIT"), FieldStatus.inferred), upstream_sources="n/a"))
    assert checks(a)["라이선스 명시 여부"].level == Level.unknown


def test_multiple_licenses_flagged():
    a = risk.license_opinion(card(license=lic("MIT", "CC-BY-NC-4.0")))
    assert checks(a)["복수 라이선스"].level == Level.medium
    assert a.level == Level.high


def test_gate_prompt_and_known_issues():
    a = risk.license_opinion(card(
        license=lic("MIT"),
        access={"type": "gated", "gate_prompt": "연구 목적으로만 사용"},
        known_issues=["Hub에서 비활성화(disabled)된 데이터셋"],
    ))
    assert checks(a)["약관·사용 제한"].level == Level.medium
    assert checks(a)["분쟁 이력"].level == Level.high


def test_pii_states():
    assert risk.privacy_opinion(card(pii_included="미포함")).level == Level.low
    assert risk.privacy_opinion(card(pii_included="포함")).level == Level.high
    assert checks(risk.privacy_opinion(card(pii_included="포함 가능성 있음")))["개인정보 포함"].level == Level.medium
    assert checks(risk.privacy_opinion(card()))["개인정보 포함"].level == Level.unknown


def test_unknown_pii_not_low_and_media_warned():
    a = risk.privacy_opinion(card(data_type="이미지"))
    assert a.level == Level.unknown
    assert "얼굴" in checks(a)["개인정보 포함"].note


def test_sensitive_types_are_high():
    a = risk.privacy_opinion(card(pii_included="포함", pii_types="이름, 건강 정보"))
    assert checks(a)["민감정보"].level == Level.high


def test_crawled_without_deid_is_medium_and_biometric_raises_us():
    a = risk.privacy_opinion(card(pii_included="포함 가능성 있음", data_type="이미지", pii_types="얼굴", creation="웹 크롤링으로 수집"))
    assert checks(a)["비식별 조치·동의"].level == Level.medium
    us = next(j for j in a.jurisdictions if j.jurisdiction == "미국")
    assert us.level == Level.high and "BIPA" in us.note


def test_deidentification_and_removal_channel_lower_risk():
    a = risk.privacy_opinion(card(
        pii_included="포함 가능성 있음",
        deidentification_consent="이름은 익명화했고 삭제 요청은 이메일로 받는다",
    ))
    assert checks(a)["비식별 조치·동의"].level == Level.low
    assert checks(a)["삭제·옵트아웃"].level == Level.low


def test_overall_follows_highest_area_and_has_three_jurisdictions():
    r = risk.analyze(card(license=lic("CC-BY-NC-4.0"), pii_included="미포함"))
    assert r.overall == Level.high
    assert [a.area for a in r.areas] == ["라이선스", "개인정보"]
    assert "라이선스, 개인정보" in r.scope_note
    assert all([j.jurisdiction for j in a.jurisdictions] == ["한국", "미국", "EU"] for a in r.areas)
    assert "법률 자문이 아니" in r.scope_note


def test_risk_api_roundtrip(monkeypatch):
    from app import watchlists as wl

    async def loader(client):
        return wl.Index([], [wl.ListStatus("us_csl", "미국 통합 제재 목록(CSL)", "미국", False, error="offline")])

    monkeypatch.setattr(main, "watch_cache", wl.IndexCache(loader))
    monkeypatch.setattr(main, "settings", main.settings.__class__())
    c = TestClient(main.app)
    import asyncio
    saved = asyncio.run(main.store.save(card(license=lic("MIT"))))
    assert c.get(f"/api/datacards/{saved.id}/risk").status_code == 404
    r = c.post(f"/api/datacards/{saved.id}/risk")
    assert r.status_code == 200, r.text
    assert [a["area"] for a in r.json()["areas"]] == ["라이선스", "개인정보", "평판(제재)", "원본 계보"]
    assert c.get(f"/api/datacards/{saved.id}/risk").json()["id"] == r.json()["id"]
    assert c.post("/api/datacards/nope/risk").status_code == 404
