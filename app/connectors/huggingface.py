"""Hugging Face Hub API 커넥터."""
from __future__ import annotations

import re

import httpx

from ..licenses import to_spdx
from ..models import CardField, DataCard, DatasetRef, FieldStatus
from .base import CollectContext, fetch_text

API = "https://huggingface.co/api/datasets"

_MODALITY = {
    "text": "텍스트",
    "audio": "오디오",
    "image": "이미지",
    "video": "비디오",
    "tabular": "표",
    "timeseries": "표",
    "3d": "멀티모달",
    "geospatial": "표",
}


def _tag_values(tags: list[str], prefix: str) -> list[str]:
    return [t[len(prefix):] for t in tags if t.startswith(prefix)]


def _strip_front_matter(readme: str) -> str:
    if readme.startswith("---"):
        end = readme.find("\n---", 3)
        if end != -1:
            return readme[end + 4:].lstrip()
    return readme


def _first_paragraph(md: str) -> str | None:
    for block in re.split(r"\n\s*\n", md):
        b = block.strip()
        if b and not b.startswith(("#", "|", "<", "!", "-", "*", "```")):
            return b[:600]
    return None


async def collect(ctx: CollectContext, ref: DatasetRef) -> DataCard:
    api_url = f"{API}/{ref.repo}" + (f"/revision/{ref.revision}" if ref.revision else "")
    src, r = await fetch_text(ctx, api_url, params={"full": "true"})
    info = r.json()
    card_data = info.get("cardData") or {}
    tags: list[str] = info.get("tags") or []
    sha = info.get("sha")
    page = f"https://huggingface.co/datasets/{ref.repo}"

    ref = ref.model_copy(update={"revision": ref.revision or sha, "url": page})
    card = DataCard(dataset=ref, sources=[src])

    readme_text = ""
    try:
        readme_src, _ = await fetch_text(ctx, f"https://huggingface.co/datasets/{ref.repo}/raw/{sha or 'main'}/README.md")
        card.sources.append(readme_src)
        readme_text = _strip_front_matter(readme_src.content)
    except httpx.HTTPError:
        pass
    readme_url = f"{page}/blob/{sha or 'main'}/README.md"

    name = card_data.get("pretty_name") or ref.repo
    card.set("name", CardField.confirmed({"official": name, "aliases": [ref.repo] if name != ref.repo else []}, page))

    card.set(
        "version",
        CardField.confirmed(
            {
                "subset": ref.version,
                "revision": sha,
                "created_at": info.get("createdAt"),
                "last_modified": info.get("lastModified"),
            },
            api_url,
        ),
    )

    desc = info.get("description") or ""
    if readme_text:
        card.set("description", CardField.confirmed({"original": readme_text[:4000]}, readme_url))
        para = _first_paragraph(readme_text)
        if para:
            card.set("overview", CardField(value=para, evidence_url=readme_url, status=FieldStatus.inferred))
    elif desc:
        card.set("description", CardField.confirmed({"original": desc}, page))

    modalities = sorted({_MODALITY.get(m, m) for m in _tag_values(tags, "modality:")})
    if len(modalities) > 1:
        modalities = ["멀티모달"] + modalities
    card.set("data_type", CardField.confirmed(modalities, api_url))

    languages = card_data.get("language") or _tag_values(tags, "language:")
    if isinstance(languages, str):
        languages = [languages]
    size = card_data.get("size_categories") or _tag_values(tags, "size_categories:")
    used_storage = info.get("usedStorage")
    card.set(
        "size_language",
        CardField.confirmed(
            {k: v for k, v in {"size_category": size, "bytes": used_storage, "languages": languages}.items() if v},
            api_url,
        ),
    )

    author = info.get("author") or ref.repo.split("/")[0]
    card.set(
        "provider",
        CardField(
            value={"name": author, "hub_profile": f"https://huggingface.co/{author}"},
            evidence_url=f"https://huggingface.co/{author}",
            status=FieldStatus.inferred,  # Hub 계정명 ≠ 실제 제작 기관일 수 있음
        ),
    )

    sources = card_data.get("source_datasets") or _tag_values(tags, "source_datasets:")
    if sources:
        card.set("upstream_sources", CardField.confirmed({"source_datasets": sources}, readme_url))

    card.set("source_url", CardField.confirmed(page, page))

    gated = info.get("gated")
    if gated:
        terms = card_data.get("extra_gated_prompt")
        card.set(
            "access",
            CardField.confirmed({"type": "로그인·동의 후 공개(gated)", "mode": gated, "gate_prompt": terms}, api_url),
        )
    else:
        card.set("access", CardField.confirmed({"type": "공개"}, api_url))

    raw_license = card_data.get("license") or _tag_values(tags, "license:")
    if isinstance(raw_license, str):
        raw_license = [raw_license]
    if raw_license:
        spdx = [to_spdx(x) for x in raw_license]
        card.set(
            "license",
            CardField.confirmed(
                {"raw": raw_license, "spdx": [s for s in spdx if s], "custom": [x for x, s in zip(raw_license, spdx) if not s]},
                readme_url,
            ),
        )
        license_files = [s["rfilename"] for s in info.get("siblings") or [] if re.match(r"(?i)^licen[sc]e", s.get("rfilename", ""))]
        evidence = [readme_url] + [f"{page}/blob/{sha or 'main'}/{f}" for f in license_files]
        card.set("license_evidence", CardField.confirmed(evidence, readme_url))

    papers = [f"https://arxiv.org/abs/{a}" for a in _tag_values(tags, "arxiv:")]
    if info.get("citation"):
        papers.append({"citation": info["citation"][:1000]})
    card.set("related_papers", CardField.confirmed(papers, api_url))

    if info.get("disabled"):
        card.set("known_issues", CardField.confirmed(["Hub에서 비활성화(disabled)된 데이터셋"], api_url))

    return card
