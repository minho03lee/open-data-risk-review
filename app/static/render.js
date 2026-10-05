// 데이터 카드 항목 값을 사람이 읽기 좋은 HTML로 바꾼다. 브라우저(window.ODR)와 node 테스트 양쪽에서 쓴다.
(function (root) {
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const LABELS = {
    official: "공식 명칭", aliases: "별칭", subset: "서브셋", revision: "리비전", created_at: "생성일", last_modified: "최종 수정일",
    last_updated: "최종 수정일", version: "버전", type: "방식", mode: "승인 방식", gate_prompt: "동의 조건", note: "비고",
    raw: "표기", spdx: "SPDX 식별자", custom: "커스텀 라이선스", bytes: "용량", languages: "언어", size_category: "규모",
    name: "이름", hub_profile: "허브 프로필", uploader: "업로더", profile: "프로필", platform: "플랫폼", terms_url: "이용약관",
    source_datasets: "상위 데이터셋", citation: "인용", summary_ko: "요약", original: "원문",
  };
  const label = (k) => LABELS[k] || k;

  const URL_RE = /(https?:\/\/[^\s<>"')\]]+[^\s<>"').,;\]])/g;
  function linkify(text) {
    return esc(text).replace(URL_RE, (u) => `<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
  }

  function humanBytes(n) {
    n = Number(n);
    if (!isFinite(n) || n <= 0) return String(n);
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
  }

  function fmtDate(s) {
    return /^\d{4}-\d{2}-\d{2}T/.test(s) ? s.slice(0, 10) : s;
  }

  // 아주 작은 마크다운 변환: 제목, 목록, 굵게, 코드, 링크, 문단. 입력은 항상 먼저 이스케이프한다.
  function inlineMd(line) {
    return esc(line)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<>"')]+[^\s<>"').,;])/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  }
  function markdown(src) {
    const out = [];
    let list = null, para = [], code = null;
    const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inlineMd).join("<br>")}</p>`); para = []; } };
    const flushList = () => { if (list) { out.push(`<ul>${list.map((i) => `<li>${inlineMd(i)}</li>`).join("")}</ul>`); list = null; } };
    for (const raw of String(src).replace(/\r/g, "").split("\n")) {
      if (raw.trim().startsWith("```")) {
        if (code) { out.push(`<pre>${esc(code.join("\n"))}</pre>`); code = null; } else { flushPara(); flushList(); code = []; }
        continue;
      }
      if (code) { code.push(raw); continue; }
      const line = raw.trimEnd();
      let m;
      if (!line.trim()) { flushPara(); flushList(); }
      else if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { flushPara(); flushList(); out.push(`<h4>${inlineMd(m[2])}</h4>`); }
      else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) { flushPara(); (list = list || []).push(m[1]); }
      else { flushList(); para.push(line); }
    }
    if (code) out.push(`<pre>${esc(code.join("\n"))}</pre>`);
    flushPara(); flushList();
    return out.join("");
  }

  const chips = (items, max = 12) => {
    const shown = items.slice(0, max).map((x) => `<span class="chip">${esc(x)}</span>`).join("");
    const rest = items.length - max;
    if (rest <= 0) return `<div class="chips">${shown}</div>`;
    const more = items.slice(max).map((x) => `<span class="chip">${esc(x)}</span>`).join("");
    return `<div class="chips">${shown}</div><details><summary>외 ${rest}개 더 보기</summary><div class="chips">${more}</div></details>`;
  };

  function scalar(v, key) {
    if (v == null || v === "") return "";
    if (typeof v === "boolean") return v ? "예" : "아니오";
    if (typeof v === "number") return key === "bytes" ? humanBytes(v) : esc(v);
    const s = String(v);
    if (key === "revision") return `<code title="${esc(s)}">${esc(s.slice(0, 10))}</code>`;
    if (key === "created_at" || key === "last_modified" || key === "last_updated") return esc(fmtDate(s));
    return s.length > 160 || s.includes("\n") ? markdown(s) : linkify(s);
  }

  function list(arr, key) {
    const items = arr.filter((x) => x != null && x !== "");
    if (!items.length) return "";
    if (items.every((x) => typeof x === "string") && items.every((x) => x.length <= 24 && !/^https?:/.test(x))) return chips(items);
    return `<ul>${items.map((x) => `<li>${typeof x === "object" ? value(x) : scalar(x, key)}</li>`).join("")}</ul>`;
  }

  const ORDER = Object.keys(LABELS);
  const rank = (k) => (ORDER.includes(k) ? ORDER.indexOf(k) : ORDER.length);

  function dict(obj) {
    const rows = Object.entries(obj)
      .sort(([a], [b]) => rank(a) - rank(b))
      .map(([k, v]) => [k, value(v, k)])
      .filter(([, html]) => html);
    if (!rows.length) return "";
    return `<dl class="kv">${rows.map(([k, html]) => `<dt>${esc(label(k))}</dt><dd>${html}</dd>`).join("")}</dl>`;
  }

  function value(v, key) {
    if (v == null) return "";
    if (Array.isArray(v)) return list(v, key);
    if (typeof v === "object") {
      // 설명: 한국어 요약을 먼저 보여 주고 원문은 접어 둔다
      if ("original" in v && typeof v.original === "string") {
        const summary = v.summary_ko ? `<div class="summary">${scalar(v.summary_ko)}</div>` : "";
        return `${summary}<details><summary>원문 보기</summary><div class="md">${markdown(v.original)}</div></details>`;
      }
      return dict(v);
    }
    return scalar(v, key);
  }

  // 긴 URL은 화면에서만 가운데를 줄여 보여 준다 (링크 자체는 원본 그대로)
  function shortUrl(u, max = 64) {
    const t = String(u).replace(/^https?:\/\//, "");
    return t.length <= max ? t : `${t.slice(0, 36)}…${t.slice(-(max - 37))}`;
  }

  const api = { esc, label, linkify, humanBytes, fmtDate, markdown, shortUrl, value };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ODR = api;
})(typeof window !== "undefined" ? window : globalThis);
