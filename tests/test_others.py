from app import others
from app.models import CardField, DataCard, DatasetRef, Platform, Source
from app.risk import Level


def card(platform=Platform.huggingface, revision="a1b2c3d4e5f6", readme="", **fields) -> DataCard:
    c = DataCard(id="c", dataset=DatasetRef(platform=platform, repo="org/ds", revision=revision, url="https://huggingface.co/datasets/org/ds"))
    if readme:
        c.sources.append(Source(url="https://huggingface.co/datasets/org/ds/blob/main/README.md", content=readme))
    for k, v in fields.items():
        c.fields[k] = CardField.confirmed(v, "https://huggingface.co/x")
    return c


def by(area):
    return {f.check: f for f in area.findings}


def test_csam_mention_is_high_with_verbatim_quote():
    a = others.other_opinion(card(readme="Removed images flagged as CSAM by the hash list.\nOther notes."))
    f = by(a)["유해 콘텐츠"]
    assert f.level == Level.high and "CSAM" in f.evidence_quote and f.evidence_url.endswith("README.md")
    assert a.level == Level.high


def test_harm_mention_is_medium_and_mitigation_alone_is_low():
    harm = by(others.other_opinion(card(readme="The corpus may contain toxic or NSFW text.")))["유해 콘텐츠"]
    assert harm.level == Level.medium
    both = by(others.other_opinion(card(readme="May contain obscene text. We removed pages with any word on the bad words list.")))["유해 콘텐츠"]
    assert both.level == Level.medium and "잔존" in both.note
    safe = by(others.other_opinion(card(readme="We applied a profanity filter to all pages.")))["유해 콘텐츠"]
    assert safe.level == Level.low and "검증되지 않았다" in safe.note


def test_generic_removed_or_filtered_is_not_a_safety_measure():
    f = by(others.other_opinion(card(readme="We removed duplicates and filtered by language.")))["유해 콘텐츠"]
    assert f.level == Level.unknown


def test_silence_is_unknown_not_low_and_crawl_is_noted():
    a = others.other_opinion(card(creation="Common Crawl 웹 크롤링으로 수집"))
    assert by(a)["유해 콘텐츠"].level == Level.unknown and "웹 수집" in by(a)["유해 콘텐츠"].note
    assert by(a)["TDM 예외·옵트아웃"].level == Level.unknown
    assert by(a)["품질·편향"].level == Level.unknown


def test_ai_act_summary_depends_on_provenance():
    assert by(others.other_opinion(card()))["EU AI Act 학습 데이터 요약"].level == Level.unknown
    assert by(others.other_opinion(card(creation="직접 녹음")))["EU AI Act 학습 데이터 요약"].level == Level.low


def test_china_signal_only_when_mentioned():
    a = others.other_opinion(card(provider_origin="본사: 중국 베이징", provider={"name": "x"}))
    assert by(a)["중국 측 규제"].level == Level.unknown and "중국" in by(a)["중국 측 규제"].evidence_quote
    assert "중국 측 규제" not in by(others.other_opinion(card()))


def test_withdrawn_is_high_and_revision_pinning():
    a = others.other_opinion(card(known_issues=["Hub에서 비활성화(disabled)된 데이터셋"]))
    assert by(a)["지속성: 철회·비공개"].level == Level.high
    assert by(a)["지속성: 버전 고정"].level == Level.low and "a1b2c3d4e5" in by(a)["지속성: 버전 고정"].note
    assert by(others.other_opinion(card(revision=None)))["지속성: 버전 고정"].level == Level.medium
    assert by(others.other_opinion(card(Platform.web, revision=None)))["지속성: 버전 고정"].level == Level.unknown


def test_quality_bias_signal_is_quoted():
    f = by(others.other_opinion(card(readme="Known limitations: the data is biased toward English.")))["품질·편향"]
    assert f.level == Level.medium and "biased" in f.evidence_quote


def test_three_jurisdictions_and_not_checked():
    a = others.other_opinion(card())
    assert [j.jurisdiction for j in a.jurisdictions] == ["한국", "미국", "EU"] and a.not_checked
