"""라이선스 표기를 SPDX 식별자로 정규화한다."""
from __future__ import annotations

import re

_ALIASES = {
    "cc0": "CC0-1.0",
    "cc0-1.0": "CC0-1.0",
    "cc0: public domain": "CC0-1.0",
    "public domain": "CC0-1.0",
    "pddl": "PDDL-1.0",
    "odc-by": "ODC-By-1.0",
    "odbl": "ODbL-1.0",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "mit": "MIT",
    "gpl-3.0": "GPL-3.0-only",
    "gpl 3": "GPL-3.0-only",
    "openrail": "OpenRAIL",
    "other": None,
    "unknown": None,
}

_CC = re.compile(r"^(?:cc|creative commons)[\s\-_]*(by(?:[\s\-_]*(?:nc|sa|nd))*)[\s\-_]*([0-9.]+)?", re.I)


def to_spdx(raw: str | None) -> str | None:
    """알려진 표기면 SPDX ID, 아니면 None (원문은 호출자가 보존)."""
    if not raw:
        return None
    s = raw.strip().lower()
    if s.startswith("license:"):
        s = s[len("license:"):]
    if s in _ALIASES:
        return _ALIASES[s]
    m = _CC.match(s)
    if m:
        terms = re.split(r"[\s\-_]+", m.group(1).upper())
        version = m.group(2) or "4.0"
        if "." not in version:
            version += ".0"
        return "CC-" + "-".join(terms) + "-" + version
    return None


def flags(spdx: str | None) -> dict[str, bool | None]:
    """상용 학습 판단에 쓰는 기본 조건. 알 수 없으면 None."""
    if not spdx:
        return {"commercial": None, "derivatives": None, "share_alike": None}
    up = spdx.upper()
    if up.startswith("CC-"):
        return {"commercial": "-NC" not in up, "derivatives": "-ND" not in up, "share_alike": "-SA" in up}
    if up in {"CC0-1.0", "PDDL-1.0", "MIT", "APACHE-2.0", "ODC-BY-1.0"}:
        return {"commercial": True, "derivatives": True, "share_alike": False}
    if up in {"ODBL-1.0", "GPL-3.0-ONLY"}:
        return {"commercial": True, "derivatives": True, "share_alike": True}
    return {"commercial": None, "derivatives": None, "share_alike": None}
