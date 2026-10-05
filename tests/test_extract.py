from app.extract import Extraction, ExtractedField, apply_extraction
from app.models import DataCard, DatasetRef, FieldStatus, Platform, Source


def _card():
    return DataCard(
        dataset=DatasetRef(platform=Platform.web, repo="example.org/corpus"),
        sources=[Source(url="https://example.org/corpus", content="Recordings were made with   consent of all participants. License: CC BY 4.0")],
    )


def test_quote_verified_marks_confirmed():
    card = apply_extraction(
        _card(),
        Extraction(
            fields=[ExtractedField(key="deidentification_consent", value="참여자 전원 동의", evidence_url="https://example.org/corpus",
                                   evidence_quote="Recordings were made with consent of all participants.")],
            description_summary_ko="", requested_documents=[],
        ),
        ["deidentification_consent"],
    )
    assert card.fields["deidentification_consent"].status == FieldStatus.confirmed


def test_unverified_quote_or_url_is_inferred():
    card = apply_extraction(
        _card(),
        Extraction(
            fields=[
                ExtractedField(key="license", value="CC-BY-4.0", evidence_url="https://example.org/corpus", evidence_quote="Licensed under CC-BY"),
                ExtractedField(key="provider", value="Example Lab", evidence_url="https://made-up.example", evidence_quote="Example Lab"),
            ],
            description_summary_ko="", requested_documents=["라이선스 원문"],
        ),
        ["license", "provider"],
    )
    assert card.fields["license"].status == FieldStatus.inferred
    assert card.fields["provider"].status == FieldStatus.inferred and card.fields["provider"].evidence_url is None
    assert "라이선스 원문" in card.needs_documents


def test_does_not_overwrite_filled_or_unrequested_fields():
    card = _card()
    card = apply_extraction(
        card,
        Extraction(fields=[ExtractedField(key="license", value="MIT", evidence_url="https://example.org/corpus", evidence_quote="License: CC BY 4.0")],
                   description_summary_ko="", requested_documents=[]),
        ["provider"],
    )
    assert card.fields["license"].is_missing


def test_enrich_request_shape():
    import json

    import anthropic
    import httpx2

    from app import extract

    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["beta"] = req.headers.get("anthropic-beta")
        out = {"fields": [{"key": "license", "value": "CC-BY-4.0", "evidence_url": "https://example.org/corpus",
                           "evidence_quote": "License: CC BY 4.0"}], "description_summary_ko": "", "requested_documents": []}
        return httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": extract.MODEL,
                                          "content": [{"type": "text", "text": json.dumps(out)}], "stop_reason": "end_turn",
                                          "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    card = extract.enrich(_card(), client)
    assert card.fields["license"].status == FieldStatus.confirmed
    assert seen["body"]["model"] == "claude-opus-5-5"
    assert seen["body"]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert 'url="https://example.org/corpus"' in seen["body"]["messages"][0]["content"]
