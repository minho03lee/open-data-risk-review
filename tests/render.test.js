const assert = require("assert");
const R = require("../app/static/render.js");

// 객체는 JSON이 아니라 한국어 라벨의 목록으로, 공식 명칭이 별칭보다 먼저
let h = R.value({ aliases: ["allenai/c4"], official: "C4" });
assert(!h.includes("{") && h.includes("<dt>공식 명칭</dt>") && h.indexOf("공식 명칭") < h.indexOf("별칭"));

// 용량·날짜·리비전 정리
h = R.value({ bytes: 33055538287189, revision: "1588ec454efa1a09f29cd18ddd04fe05fc8653a2", created_at: "2022-03-02T23:29:22.000Z" });
assert(h.includes("30.1 TB") && h.includes("2022-03-02") && h.includes("1588ec454e") && !h.includes("23:29"));

// 긴 언어 목록은 접는다
h = R.value({ languages: Array.from({ length: 30 }, (_, i) => "l" + i) });
assert(h.includes("외 18개 더 보기"));

// 설명: 요약 먼저, 원문은 접어서 마크다운으로
h = R.value({ original: "# C4\n\n- **Paper:** https://arxiv.org/abs/1910.10683\n\nbody", summary_ko: "요약" });
assert(h.indexOf("요약") < h.indexOf("원문 보기") && h.includes("<h4>C4</h4>") && h.includes("<strong>Paper:</strong>") && h.includes('href="https://arxiv.org/abs/1910.10683"'));

// 사용자 입력은 항상 이스케이프
h = R.value({ original: '<img src=x onerror=alert(1)> [a](javascript:alert(1)) "q"', summary_ko: "<b>x</b>" });
assert(!h.includes("<img") && !h.includes("<b>") && !h.includes('href="javascript'));
assert(R.value('x"><script>alert(1)</script>').indexOf("<script>") === -1);

// 문자열의 URL은 링크로, 빈 값은 빈 문자열
assert(R.value("약관: https://commoncrawl.org/terms-of-use/ 적용").includes('<a href="https://commoncrawl.org/terms-of-use/"'));
assert.strictEqual(R.value(null), "");
assert.strictEqual(R.value({ subset: null }), "");
assert.strictEqual(R.value([]), "");

// URL 줄임
assert(R.shortUrl("https://huggingface.co/datasets/allenai/c4/blob/1588ec454efa1a09f29cd18ddd04fe05fc8653a2/README.md").includes("…"));
assert.strictEqual(R.shortUrl("https://a.org/x"), "a.org/x");
console.log("render ok");
