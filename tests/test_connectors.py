import json
from pathlib import Path

import httpx

from app.connectors import CollectContext, collect
from app.identify import parse_url
from app.models import FieldStatus

FIX = Path(__file__).parent / "fixtures"


def hf_handler(req: httpx.Request):
    if req.url.path == "/api/datasets/allenai/c4":
        return httpx.Response(200, json=json.loads((FIX / "hf_c4.json").read_text()))
    if req.url.path.endswith("/README.md"):
        return httpx.Response(200, text=(FIX / "hf_c4_readme.md").read_text())
    return httpx.Response(404)


async def test_huggingface_card():
    async with httpx.AsyncClient(transport=httpx.MockTransport(hf_handler)) as client:
        card = await collect(CollectContext(client=client), parse_url("https://huggingface.co/datasets/allenai/c4"))
    f = card.fields
    assert card.dataset.revision == "1588ec454efa1a09f29cd18ddd04fe05fc8653a2"
    assert f["name"].value["official"] == "C4"
    assert f["license"].value["spdx"] == ["ODC-By-1.0"]
    assert f["license"].status == FieldStatus.confirmed
    assert any(u.endswith("/LICENSE") for u in f["license_evidence"].value)
    assert f["data_type"].value == ["텍스트"]
    assert f["access"].value["type"] == "공개"
    assert "https://arxiv.org/abs/1910.10683" in f["related_papers"].value
    assert f["provider"].status == FieldStatus.inferred
    assert f["overview"].value.startswith("A colossal")
    assert f["pii_included"].is_missing  # 메타데이터만으로는 채우지 않는다
    assert len(card.sources) == 2


async def test_gated_dataset():
    data = json.loads((FIX / "hf_c4.json").read_text())
    data.update(gated="manual", cardData={**data["cardData"], "extra_gated_prompt": "You agree to not use for commercial purposes."})

    def handler(req):
        if req.url.path == "/api/datasets/allenai/c4":
            return httpx.Response(200, json=data)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        card = await collect(CollectContext(client=client), parse_url("https://huggingface.co/datasets/allenai/c4"))
    assert card.fields["access"].value["type"] == "로그인·동의 후 공개(gated)"
    assert "commercial" in card.fields["access"].value["gate_prompt"]


async def test_kaggle_api():
    def handler(req):
        assert req.headers.get("authorization", "").startswith("Basic ")
        return httpx.Response(200, json={"title": "Wine Reviews", "subtitle": "130k wine reviews", "licenseName": "CC BY-NC-SA 4.0",
                                         "ownerName": "zackthoutt", "currentVersionNumber": 4, "totalBytes": 51000000})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        card = await collect(CollectContext(client=client, kaggle_auth=("u", "k")), parse_url("https://www.kaggle.com/datasets/zynicide/wine-reviews"))
    assert card.dataset.version == "4"
    assert card.fields["license"].value["spdx"] == ["CC-BY-NC-SA-4.0"]
    assert card.fields["provider"].status == FieldStatus.inferred


async def test_kaggle_without_key_asks_for_docs():
    html = '<html><head><title>Wine Reviews | Kaggle</title><meta name="description" content="130k wine reviews"></head></html>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, html=html))) as client:
        card = await collect(CollectContext(client=client), parse_url("https://www.kaggle.com/datasets/zynicide/wine-reviews"))
    assert card.fields["license"].is_missing
    assert card.needs_documents


async def test_aihub_page():
    html = '<html><head><title>한국어 음성 - AI-Hub</title></head><body><script>x()</script><p>구축기관: 한국과학기술원</p></body></html>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, html=html))) as client:
        card = await collect(CollectContext(client=client), parse_url("https://www.aihub.or.kr/aihubdata/data/view.do?dataSetSn=123"))
    assert card.fields["name"].value["official"] == "한국어 음성"
    assert card.fields["access"].value["type"] == "신청제"
    assert "x()" not in card.sources[0].content and "한국과학기술원" in card.sources[0].content


async def test_web_page():
    html = '<html><head><meta property="og:title" content="Example Corpus"><meta name="description" content="A speech corpus"></head><body>License: CC BY 4.0</body></html>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, html=html))) as client:
        card = await collect(CollectContext(client=client), parse_url("https://example.org/corpus"))
    assert card.fields["name"].status == FieldStatus.inferred
    assert "License: CC BY 4.0" in card.sources[0].content
