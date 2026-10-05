import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app import main
from tests.test_connectors import hf_handler


def _patch_http(monkeypatch, handler):
    monkeypatch.setattr(main, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_create_get_patch_card(monkeypatch):
    _patch_http(monkeypatch, hf_handler)
    c = TestClient(main.app)
    r = c.post("/api/datacards", json={"query": "https://huggingface.co/datasets/allenai/c4"})
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["fields"]["license"]["value"]["spdx"] == ["ODC-By-1.0"]

    r = c.patch(f"/api/datacards/{card['id']}", json={"fields": {"pii_included": "포함 가능성 있음"}})
    assert r.json()["fields"]["pii_included"]["status"] == "사용자 입력"
    assert c.get(f"/api/datacards/{card['id']}").json()["fields"]["pii_included"]["value"] == "포함 가능성 있음"
    assert c.get("/api/datacards").json()[0]["id"] == card["id"]


def test_ambiguous_name_returns_candidates(monkeypatch):
    _patch_http(monkeypatch, lambda r: httpx.Response(200, json=[{"id": "laion/laion400m"}, {"id": "laion/laion2B-en"}]))
    r = TestClient(main.app).post("/api/datacards", json={"query": "LAION"})
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "candidates"


def test_private_dataset_asks_for_upload(monkeypatch):
    _patch_http(monkeypatch, lambda r: httpx.Response(401))
    r = TestClient(main.app).post("/api/datacards", json={"query": "https://huggingface.co/datasets/org/private"})
    assert r.status_code == 422


def test_token_required(monkeypatch):
    monkeypatch.setattr(main, "settings", main.settings.__class__(access_token="s3cret"))
    c = TestClient(main.app)
    assert c.get("/api/datacards").status_code == 401
    assert c.get("/api/datacards", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_card_is_still_created_when_claude_api_fails(monkeypatch):
    import anthropic
    _patch_http(monkeypatch, hf_handler)
    monkeypatch.setattr(main, "settings", main.settings.__class__(anthropic_api_key="k"))
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("Your credit balance is too low", response=httpx.Response(400, request=req), body=None)
    def boom(card):
        raise err
    monkeypatch.setattr(main.extract, "enrich", boom)
    r = TestClient(main.app).post("/api/datacards", json={"query": "https://huggingface.co/datasets/allenai/c4"})
    assert r.status_code == 200, r.text
    assert any("크레딧이 부족" in n for n in r.json()["needs_documents"])
