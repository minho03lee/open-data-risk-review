import httpx

from app import watchlists as wl

_COLS = ["_id", "source", "entity_number", "type", "programs", "name", "title", "addresses", "federal_register_notice", "start_date",
         "end_date", "standard_order", "license_requirement", "license_policy", "call_sign", "vessel_type", "gross_tonnage",
         "gross_registered_tonnage", "vessel_flag", "vessel_owner", "remarks", "source_list_url", "alt_names", "citizenships",
         "dates_of_birth", "nationalities", "places_of_birth", "source_information_url", "ids"]


def _csl(rows):
    import csv, io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_COLS)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


CSL = _csl([
    {"_id": "1", "source": "Entity List (EL) - Bureau of Industry and Security", "type": "Entity",
     "name": "Example Frontier Holdings Co., Ltd.", "addresses": "1 Fake Road, Nowhere", "remarks": "Fictional entry for tests",
     "source_list_url": "https://example.test/el", "alt_names": "Frontier Example Group;EFH Ltd"},
    {"_id": "2", "source": "Specially Designated Nationals (SDN) - Treasury Department", "type": "Entity", "programs": "RUSSIA-EO14024",
     "name": "Sample Sanctioned Bank PJSC", "source_list_url": "https://example.test/sdn"},
])

EU = """﻿"Entity_LogicalId";"Entity_Regulation_Programme";"NameAlias_WholeName"
"101";"UKR";"Sample Eastern Trading LLC"
"101";"UKR";"SET Trading"
"102";"IRN";"Placeholder Maritime Corporation"
"""


def test_norm_strips_punctuation_and_suffixes():
    assert wl.norm("Example Frontier Holdings Co., Ltd.") == "example frontier holdings"
    assert wl.norm("주식회사 샘플") == "샘플"


def test_parse_csl_and_eu():
    csl = wl.parse_csl(CSL)
    assert [e.name for e in csl][0].startswith("Example Frontier")
    assert csl[0].aliases == ["Frontier Example Group", "EFH Ltd"]
    assert csl[0].jurisdiction == "미국" and csl[0].source_url == "https://example.test/el"
    eu = wl.parse_eu(EU)
    assert len(eu) == 2 and eu[0].aliases == ["SET Trading"] and eu[0].jurisdiction == "EU"


def test_eu_with_unexpected_columns_raises():
    try:
        wl.parse_eu("a;b\n1;2\n")
    except ValueError:
        return
    raise AssertionError("열 구성이 다르면 실패로 보고해야 한다")


def index():
    return wl.Index(wl.parse_csl(CSL) + wl.parse_eu(EU), [])


def test_exact_alias_and_similar_matches():
    idx = index()
    assert idx.match("Example Frontier Holdings")[0].kind == "exact"
    assert idx.match("frontier example group")[0].kind == "exact"  # 별칭
    sim = idx.match("Example Frontier Holding")  # 한 글자 차이
    assert sim and sim[0].kind == "similar"  # 한 글자 차이는 유사 일치
    close = idx.match("Example Frontier Holdngs")
    assert close and close[0].kind == "similar" and close[0].score >= 0.88


def test_unrelated_and_short_names_do_not_match():
    idx = index()
    assert idx.match("Allen Institute for AI") == []
    assert idx.match("EFHX") == []  # 짧은 이름은 정확 일치만


def test_manual_lists_require_source_url(tmp_path):
    (tmp_path / "x.csv").write_text(
        "list,jurisdiction,name,aliases,program,source_url,note\n"
        "테스트 목록,한국,가상기관,별칭A;별칭B,프로그램,https://example.test/kr,\n"
        "테스트 목록,한국,근거없음,,,,\n", encoding="utf-8")
    entries, files = wl.load_manual(tmp_path)
    assert files == 1 and [e.name for e in entries] == ["가상기관"]
    assert entries[0].aliases == ["별칭A", "별칭B"] and entries[0].jurisdiction == "한국"


async def test_build_index_reports_failures_without_breaking(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        if "trade.gov" in req.url.host:
            return httpx.Response(200, text=CSL)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        idx = await wl.build_index(c, manual_dir=tmp_path)
    by = {s.list_id: s for s in idx.statuses}
    assert by["us_csl"].ok and by["us_csl"].count == 2
    assert not by["eu_fsf"].ok and "503" in by["eu_fsf"].error
    assert [s.list_id for s in idx.loaded] == ["us_csl"]
