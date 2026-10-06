"""종합 의견: 영역별 분석 결과를 한 번에 읽히도록 정리한다.

규칙 기반으로 이미 나온 등급·쟁점·근거·권고를 모아 순서만 정해 보여 주고, 새로운 사실이나 근거를 만들어 내지 않는다.
등급 문구는 '법률 자문이 아닌 1차 검토'를 전제로 한 초안이며 법무 검토가 필요하다.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .risk import AreaOpinion, Finding, Level, LineageNode, worst

_RANK = {Level.high: 3, Level.medium: 2, Level.unknown: 1, Level.low: 0}
MAX_POINTS = 3
MAX_PRIORITIES = 6
MAX_CAVEATS = 6


class Point(BaseModel):
    check: str
    level: Level
    text: str
    evidence_url: str | None = None


class AreaBrief(BaseModel):
    area: str
    level: Level
    headline: str
    points: list[Point] = Field(default_factory=list)


class OverallOpinion(BaseModel):
    level: Level
    verdict: str
    drivers: list[str] = Field(default_factory=list)  # 종합 등급을 정한 영역
    areas: list[AreaBrief] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)  # 먼저 할 일
    caveats: list[str] = Field(default_factory=list)  # 아직 확인하지 않은 것과 한계


VERDICTS = {
    Level.high: "현재 근거로는 상용 모델 학습에 그대로 쓰는 것을 권고하지 않습니다. 높음으로 나온 쟁점을 해소하거나 법무 승인을 받은 뒤 사용 여부를 정하세요.",
    Level.medium: "조건부로만 검토할 수 있습니다. 중간으로 나온 쟁점을 확인·해소한 뒤 사용 여부를 정하세요.",
    Level.unknown: "낮다고 판단할 근거가 부족합니다. 아래 확인 사항을 해소하기 전에는 사용 가능하다고 보지 마세요.",
    Level.low: "확인한 범위에서는 뚜렷한 쟁점이 없습니다. 다만 확인하지 않은 범위가 남아 있으니 아래 한계를 함께 보세요.",
}


def _first_sentence(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.+?[.다요])(\s|$)", text)
    out = m.group(1) if m else text
    return out if len(out) <= limit else out[: limit - 1] + "…"


def _points(findings: list[Finding], headline: str = "") -> list[Point]:
    shown = [f for f in findings if f.level != Level.low]
    shown.sort(key=lambda f: -_RANK[f.level])  # 정렬은 안정적이라 같은 등급 안에서는 원래 순서를 지킨다
    # 영역 요지와 같은 문장이 쟁점으로 또 나오지 않게 한다
    shown = [f for f in shown if _first_sentence(f.note) != _first_sentence(headline)]
    return [Point(check=f.check, level=f.level, text=_first_sentence(f.note), evidence_url=f.evidence_url) for f in shown[:MAX_POINTS]]


def summarize(areas: list[AreaOpinion], lineage: list[LineageNode] | None = None) -> OverallOpinion:
    level = worst([a.level for a in areas])
    ordered = sorted(areas, key=lambda a: -_RANK[a.level])
    drivers = [a.area for a in ordered if a.level == level and level != Level.low]

    briefs = [AreaBrief(area=a.area, level=a.level, headline=a.summary, points=_points(a.findings, a.summary)) for a in areas]

    priorities: list[str] = []
    for a in ordered:
        if a.level == Level.low:
            continue
        for r in a.recommendations:
            line = f"[{a.area}] {r}"
            if line not in priorities:
                priorities.append(line)
    priorities = priorities[:MAX_PRIORITIES]

    caveats: list[str] = []
    for a in areas:
        for x in a.not_checked:
            if x not in caveats:
                caveats.append(x)
    caveats = caveats[:MAX_CAVEATS]
    caveats.append("이 의견은 법률 자문이 아닌 1차 검토이며, 법역별 문구·유형별 규칙·제재 목록은 법무 검토가 필요한 초안입니다.")

    verdict = VERDICTS[level]
    if drivers:
        verdict += f" (종합 등급은 {', '.join(drivers)} 영역이 정했습니다.)"
    return OverallOpinion(level=level, verdict=verdict, drivers=drivers, areas=briefs, priorities=priorities, caveats=caveats)
