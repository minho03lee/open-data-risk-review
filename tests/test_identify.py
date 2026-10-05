import httpx
import pytest

from app.identify import decide, identify, parse_url
from app.models import Candidate, DatasetRef, Platform


@pytest.mark.parametrize(
    "url,platform,repo,version,revision",
    [
        ("https://huggingface.co/datasets/allenai/c4", Platform.huggingface, "allenai/c4", None, None),
        ("https://huggingface.co/datasets/allenai/c4/tree/abc123", Platform.huggingface, "allenai/c4", None, "abc123"),
        ("https://hf.co/datasets/allenai/c4?subset=en", Platform.huggingface, "allenai/c4", "en", None),
        ("https://www.kaggle.com/datasets/zynicide/wine-reviews", Platform.kaggle, "zynicide/wine-reviews", None, None),
        ("https://www.kaggle.com/datasets/zynicide/wine-reviews/versions/4", Platform.kaggle, "zynicide/wine-reviews", "4", None),
        ("https://www.aihub.or.kr/aihubdata/data/view.do?currMenu=115&dataSetSn=71", Platform.aihub, "71", None, None),
        ("https://commoncrawl.org/get-started", Platform.web, "commoncrawl.org/get-started", None, None),
    ],
)
def test_parse_url(url, platform, repo, version, revision):
    ref = parse_url(url)
    assert (ref.platform, ref.repo, ref.version, ref.revision) == (platform, repo, version, revision)


def test_parse_url_non_url():
    assert parse_url("LAION") is None


def _cand(repo, score):
    return Candidate(ref=DatasetRef(platform=Platform.huggingface, repo=repo), title=repo, score=score)


def test_decide_rules():
    assert decide("x", []).status == "need_info"
    assert decide("c4", [_cand("allenai/c4", 1.0), _cand("x/c4-mini", 0.6)]).status == "confirm"
    many = decide("laion", [_cand("laion/laion400m", 0.6), _cand("laion/laion5b", 0.55)])
    assert many.status == "candidates" and len(many.candidates) == 2


async def test_identify_name_search():
    def handler(req: httpx.Request):
        assert req.url.path == "/api/datasets"
        return httpx.Response(200, json=[{"id": "allenai/c4", "author": "allenai"}, {"id": "someone/c4-small", "author": "someone"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await identify(client, "allenai/c4")
    assert result.status == "confirm"
    assert result.ref.repo == "allenai/c4"
