import httpx
import pytest

from app import news
from app.models import CardField, DataCard, DatasetRef, Platform
from app.risk import Level


def card(repo="acme/MegaCorpus-X", name=None) -> DataCard:
    c = DataCard(id="news-card", dataset=DatasetRef(platform=Platform.huggingface, repo=repo))
    if name:
        c.fields["name"] = CardField.confirmed(name, "https://huggingface.co/x")
    return c


def art(title, url="https://news.example/a1", domain="news.example", seen="20261001T000000Z"):
    return news.Article(title=title, url=url, domain=domain, seen=seen)


def test_negative_words_match_whole_words_only():
    assert news.is_negative("Authors sue AI firm over dataset") == "sue"
    assert news.is_negative("Lawsuit filed over MegaCorpus") == "lawsuit"
    assert news.is_negative("LAION 데이터셋 저작권 논란") == "저작권"
    assert news.is_negative("A new issue in urban refined data") is None


def test_queries_use_name_and_providers_once():
    qs = news.queries_for(card(name="MegaCorpus X"), ["Acme", "acme", "Globex"])
    names = [n for n, _ in qs]
    assert names[0] == "MegaCorpus X" and "Acme" in names
    assert len(names) == len(set(n.lower() for n in names)) <= news.MAX_QUERIES
    assert all(q.startswith('"') for _, q in qs)


async def test_negative_article_is_medium_with_verbatim_evidence():
    async def search(q):
        return [art("MegaCorpus-X dataset hit by copyright lawsuit"), art("Weather report", url="https://news.example/a2")]
    out = await news.news_findings(card(), ["Acme"], search)
    hit = next(f for f in out if f.check == "언론 보도: 부정 기사")
    assert hit.level == Level.medium
    assert hit.evidence_url == "https://news.example/a1"
    assert hit.evidence_quote == "MegaCorpus-X dataset hit by copyright lawsuit"
    assert "2026-10-01" in hit.note


async def test_article_without_name_in_title_is_ignored():
    async def search(q):
        return [art("Big lawsuit against someone else entirely")]
    out = await news.news_findings(card(), [], search)
    assert not any(f.check == "언론 보도: 부정 기사" for f in out)


async def test_no_hits_is_unknown_never_low():
    async def search(q):
        return []
    out = await news.news_findings(card(), ["Acme"], search)
    assert out and all(f.level == Level.unknown for f in out)
    assert "3개월" in out[0].note


async def test_search_failure_is_unknown_and_reported():
    async def search(q):
        raise news.SearchError("ConnectTimeout")
    out = await news.news_findings(card(), [], search)
    assert len(out) == 1 and out[0].level == Level.unknown and "ConnectTimeout" in out[0].note


async def test_partial_failure_keeps_other_results():
    async def search(q):
        if "Acme" in q:
            raise news.SearchError("503")
        return [art("MegaCorpus-X scandal erupts")]
    out = await news.news_findings(card(), ["Acme"], search)
    levels = {f.level for f in out}
    assert Level.medium in levels and Level.unknown in levels


async def test_short_name_hit_needs_human_check():
    async def search(q):
        return [art("C4 lawsuit shocks investors", url="https://news.example/c4")]
    out = await news.news_findings(card(repo="allenai/c4"), [], search)
    hit = next(f for f in out if f.check == "언론 보도: 부정 기사")
    assert hit.level == Level.unknown and "동명" not in hit.note and "흔해" in hit.note


async def test_no_names_means_unknown():
    c = card(repo="x")
    c.dataset.repo = ""
    out = await news.news_findings(c, [], lambda q: [])
    assert out[0].level == Level.unknown


async def test_gdelt_client_parses_caches_and_maps_errors(monkeypatch):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.params["query"])
        if "boom" in req.url.params["query"]:
            return httpx.Response(200, text="Your query was too short.")
        if "down" in req.url.params["query"]:
            return httpx.Response(503)
        return httpx.Response(200, json={"articles": [{"title": "T", "url": "https://n/1", "domain": "n", "seendate": "20261002T010203Z"}, {"url": "x"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(news.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(news, "MIN_INTERVAL", 0)
    g = news.GdeltClient()
    a = await g.search('"ok"')
    assert [x.title for x in a] == ["T"] and a[0].seen == "20261002"
    await g.search('"ok"')
    assert calls.count('"ok"') == 1  # 캐시
    with pytest.raises(news.SearchError):
        await g.search('"boom"')
    with pytest.raises(news.SearchError):
        await g.search('"down"')


def test_reputation_area_merges_news_and_drops_not_checked():
    from app import reputation as rp
    from app import watchlists as wl
    from app.risk import Finding
    c = card()
    idx = wl.Index([], [])
    plain = rp.opinion(c, idx, [])
    assert any("언론 보도" in x for x in plain.not_checked)
    hit = Finding(check="언론 보도: 부정 기사", level=Level.medium, note="n", evidence_url="https://n/1", evidence_quote="t")
    with_news = rp.opinion(c, idx, [], [hit])
    assert hit in with_news.findings
    assert not any(x.startswith("언론 보도") for x in with_news.not_checked)
    assert with_news.level in (Level.medium, Level.high)


async def test_gdelt_retries_once_on_429_and_reports_status(monkeypatch):
    seq = iter([429, 200])
    def ok(req):
        return httpx.Response(next(seq), json={"articles": [{"title": "T", "url": "https://n/1"}]})
    real = httpx.AsyncClient
    monkeypatch.setattr(news.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(ok)))
    monkeypatch.setattr(news, "MIN_INTERVAL", 0)
    assert [a.title for a in await news.GdeltClient().search('"x"')] == ["T"]

    monkeypatch.setattr(news.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="forbidden"))))
    with pytest.raises(news.SearchError, match="HTTP 403 forbidden"):
        await news.GdeltClient().search('"y"')
