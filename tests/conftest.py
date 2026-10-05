import pytest

from app import main, news


@pytest.fixture(autouse=True)
def _no_real_news(monkeypatch):
    """테스트는 실제 GDELT를 호출하지 않는다. 검색은 기본적으로 '결과 없음'을 돌려준다."""
    async def empty(query):
        return []
    monkeypatch.setattr(main.gdelt, "search", empty)
