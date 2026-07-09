# -*- coding: utf-8 -*-
"""카드 정본 뷰어 생성기 — cards.json을 자체완결 HTML로 임베드 (민옥 검수용).

실행:  python eval/카드뷰어_생성.py
출력:  eval/카드뷰어.html  (더블클릭으로 열림 — 서버 불필요, 외부 리소스 0)

데이터가 바뀌면(merge.py 재실행 등) 이 스크립트를 다시 돌리면 된다.
"""
import json
import os
import sys
import collections
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS_PATH = os.path.join(ROOT, "data", "processed", "cards.json")
OUT_PATH = os.path.join(ROOT, "eval", "카드뷰어.html")

# 태그 사전: tagdict(태그사전v2.json 또는 내장 픽스처)에서 — 실패 시 최소 폴백
sys.path.insert(0, ROOT)
try:
    from app.tagdict import active_tags, is_hard
    DICT_TAGS = list(active_tags())
    HARD_TAGS = [t for t in DICT_TAGS if is_hard(t)]
except Exception as e:
    print("warn: tagdict import failed (%s) - fallback dict" % type(e).__name__)
    DICT_TAGS = ["오션뷰", "산방산뷰", "숲뷰", "노을", "감성", "조용함", "대형", "베이커리",
                 "브런치", "디저트", "애견동반", "노키즈존", "키즈친화", "통창", "야외석",
                 "루프탑", "주차편함", "웨이팅", "신상", "로컬", "포토존", "핸드드립", "한라산뷰"]
    HARD_TAGS = ["애견동반", "노키즈존", "키즈친화"]

cards = json.load(open(CARDS_PATH, encoding="utf-8"))

# ---- 통계 ----
n_total = len(cards)
n_closed = sum(1 for c in cards if c.get("closed"))
n_serving = sum(1 for c in cards if not c.get("closed") and c.get("판정") == "유지")
n_hold = n_total - n_serving - n_closed  # 보류/제외(판정 비유지·비폐업)
n_pid = sum(1 for c in cards if c.get("place_id"))
n_geo = sum(1 for c in cards if c.get("lat"))

# ---- 태그 노이즈 맵: 사전 태그별 변형(부분문자열 관계) 공존 실측 ----
tag_freq = collections.Counter()
for c in cards:
    for t in (c.get("tags") or []):
        tag_freq[t] += 1
noise = {}
for T in DICT_TAGS:
    variants = {t: n for t, n in tag_freq.items()
                if t != T and (T in t or t in T)}
    if variants:
        noise[T] = {"정식": tag_freq.get(T, 0),
                    "변형": dict(sorted(variants.items(), key=lambda x: -x[1])[:8])}

generated = datetime.now().strftime("%Y-%m-%d %H:%M")

HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>카드 정본 뷰어 — 카페 인 제주</title>
<style>
:root{
  --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --s1:#2a78d6; --s1-tint:rgba(42,120,214,.12);
  --good:#0ca30c; --warn:#fab219; --warn-tint:rgba(250,178,25,.16);
  --crit:#d03b3b; --crit-tint:rgba(208,59,59,.12);
}
@media (prefers-color-scheme: dark){
  :root{
    --surface:#1a1a19; --page:#0d0d0d; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --s1:#3987e5; --s1-tint:rgba(57,135,229,.18);
    --crit:#d03b3b; --crit-tint:rgba(208,59,59,.24); --warn-tint:rgba(250,178,25,.20);
  }
}
*{box-sizing:border-box; margin:0}
body{background:var(--page); color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; padding:20px}
h1{font-size:18px; font-weight:700}
h2{font-size:13px; font-weight:600; color:var(--ink2); margin-bottom:10px}
.sub{color:var(--muted); font-size:12px; margin:4px 0 18px}
.card{background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px}
.grid{display:grid; gap:12px}
.tiles{grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); margin-bottom:12px}
.tile .v{font-size:26px; font-weight:700}
.tile .l{font-size:12px; color:var(--ink2); margin-top:2px}
.charts{grid-template-columns:1fr 1fr; margin-bottom:12px}
@media(max-width:900px){.charts{grid-template-columns:1fr}}
.bar-row{display:grid; grid-template-columns:76px 1fr 52px; align-items:center;
  gap:8px; padding:1px 0; cursor:pointer; border-radius:4px}
.bar-row:hover{background:var(--s1-tint)}
.bar-row.on .bar{outline:2px solid var(--ink)}
.bar-label{font-size:12px; color:var(--ink2); text-align:right; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis}
.bar-track{height:16px; position:relative}
.bar{height:16px; background:var(--s1); border-radius:0 4px 4px 0; min-width:2px}
.bar.other{background:var(--muted)}
.bar-val{font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums}
.legend{display:flex; gap:14px; font-size:12px; color:var(--ink2); margin:8px 0 0}
.legend i{display:inline-block; width:10px; height:10px; border-radius:2px;
  margin-right:5px; vertical-align:-1px}
.noise{margin-bottom:12px}
.noise-row{display:flex; flex-wrap:wrap; gap:6px; align-items:center;
  padding:5px 0; border-bottom:1px solid var(--grid); font-size:13px}
.noise-row:last-child{border-bottom:0}
.noise-tag{font-weight:600; min-width:88px}
.chip{display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px;
  background:var(--s1-tint); color:var(--ink)}
.chip.off{background:transparent; border:1px solid var(--grid); color:var(--muted)}
.chip.hard{box-shadow:inset 0 0 0 1px var(--s1)}
.filters{display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; position:sticky;
  top:0; background:var(--page); padding:10px 0; z-index:5; align-items:center}
.filters input,.filters select,.filters button{font:inherit; color:var(--ink);
  background:var(--surface); border:1px solid var(--axis); border-radius:8px; padding:6px 10px}
.filters input{flex:1; min-width:180px}
.filters button{cursor:pointer}
#count{font-size:12px; color:var(--muted); white-space:nowrap}
.spot{background:var(--surface); border:1px solid var(--border); border-radius:10px;
  padding:12px 14px; margin-bottom:8px; content-visibility:auto; contain-intrinsic-size:120px}
.spot-head{display:flex; flex-wrap:wrap; gap:8px; align-items:baseline}
.spot-name{font-weight:700; font-size:15px}
.spot-region{color:var(--ink2); font-size:12px}
.badge{font-size:11px; font-weight:600; padding:1px 7px; border-radius:9px}
.badge.closed{background:var(--crit-tint); color:var(--crit); border:1px solid var(--crit)}
.badge.hold{background:var(--warn-tint); color:var(--ink); border:1px solid var(--warn)}
.spot-tags{margin:6px 0 4px; display:flex; flex-wrap:wrap; gap:4px}
.summary{color:var(--ink2)}
.meta{font-size:12px; color:var(--muted); margin-top:6px; display:flex;
  flex-wrap:wrap; gap:4px 14px; font-variant-numeric:tabular-nums}
.meta b{color:var(--ink2); font-weight:600}
details{margin-top:6px}
summary{font-size:12px; color:var(--muted); cursor:pointer}
pre{font-size:11px; background:var(--page); border:1px solid var(--grid);
  border-radius:8px; padding:10px; overflow-x:auto; margin-top:6px}
.caution{color:var(--ink2)}
</style>
</head>
<body>
<h1>카드 정본 뷰어 — 카페 인 제주</h1>
<div class="sub">생성 __GENERATED__ · 원본 data/processed/cards.json (__TOTAL__장) ·
갱신하려면 <code>python eval/카드뷰어_생성.py</code> 재실행</div>

<div class="grid tiles">
  <div class="card tile"><div class="v">__TOTAL__</div><div class="l">전체 카드</div></div>
  <div class="card tile"><div class="v">__SERVING__</div><div class="l">서빙 (유지·비폐업)</div></div>
  <div class="card tile"><div class="v">__HOLD__</div><div class="l">보류·제외</div></div>
  <div class="card tile"><div class="v">__CLOSED__</div><div class="l">폐업</div></div>
  <div class="card tile"><div class="v">__PID__</div><div class="l">place_id 보유</div></div>
  <div class="card tile"><div class="v">__GEO__</div><div class="l">좌표 보유</div></div>
</div>

<div class="grid charts">
  <div class="card">
    <h2>지역 버킷 분포 <span style="color:var(--muted);font-weight:400">(막대 클릭 = 필터)</span></h2>
    <div id="regionChart"></div>
  </div>
  <div class="card">
    <h2>태그 빈도 상위 25 <span style="color:var(--muted);font-weight:400">(막대 클릭 = 필터)</span></h2>
    <div id="tagChart"></div>
    <div class="legend"><span><i style="background:var(--s1)"></i>사전 태그 (23)</span>
      <span><i style="background:var(--muted)"></i>사전 밖 (merge union 유입)</span></div>
  </div>
</div>

<div class="card noise">
  <h2>태그 노이즈 — 사전 태그 vs 카드 변형 공존 (merge.py 정규화 후보)</h2>
  <div id="noisePanel"></div>
</div>

<div class="filters">
  <input id="q" placeholder="검색: 이름 · 별칭 · 요약 · 태그">
  <select id="fRegion"><option value="">지역: 전체</option></select>
  <select id="fTag"><option value="">태그: 전체</option></select>
  <select id="fState">
    <option value="">상태: 전체</option><option value="serving">서빙만</option>
    <option value="hold">보류·제외</option><option value="closed">폐업</option>
  </select>
  <select id="fSort">
    <option value="bloggers">정렬: 블로거↓</option><option value="mention">언급↓</option>
    <option value="rating">별점↓</option><option value="name">이름순</option>
  </select>
  <button id="reset">초기화</button>
  <span id="count"></span>
</div>
<div id="list"></div>

<script>
const CARDS = __CARDS__;
const DICT = new Set(__DICT__);
const HARD = new Set(__HARD__);
const NOISE = __NOISE__;

const esc = s => String(s ?? "").replace(/[&<>"]/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m]));
const bucket = c => c.region_bucket || "미상";

// ---- 차트 (전체 코퍼스 기준, 클릭 = 리스트 필터) ----
function barChart(el, rows, onClick, colorFn){
  const max = Math.max(...rows.map(r => r.n));
  el.innerHTML = rows.map(r =>
    `<div class="bar-row" data-key="${esc(r.key)}" title="${esc(r.key)}: ${r.n}장">
       <div class="bar-label">${esc(r.key)}</div>
       <div class="bar-track"><div class="bar${colorFn && !colorFn(r) ? " other" : ""}"
         style="width:${(100 * r.n / max).toFixed(1)}%"></div></div>
       <div class="bar-val">${r.n}</div></div>`).join("");
  el.querySelectorAll(".bar-row").forEach(row => row.onclick = () => {
    const on = row.classList.contains("on");
    el.querySelectorAll(".bar-row").forEach(x => x.classList.remove("on"));
    if (!on) row.classList.add("on");
    onClick(on ? "" : row.dataset.key);
  });
}
const regionCount = {};
CARDS.forEach(c => regionCount[bucket(c)] = (regionCount[bucket(c)] || 0) + 1);
barChart(document.getElementById("regionChart"),
  Object.entries(regionCount).map(([key, n]) => ({key, n})).sort((a, b) => b.n - a.n),
  key => { F.region = key; document.getElementById("fRegion").value = key; render(); });

const tagCount = {};
CARDS.forEach(c => (c.tags || []).forEach(t => tagCount[t] = (tagCount[t] || 0) + 1));
barChart(document.getElementById("tagChart"),
  Object.entries(tagCount).map(([key, n]) => ({key, n})).sort((a, b) => b.n - a.n).slice(0, 25),
  key => { F.tag = key; document.getElementById("fTag").value = key; render(); },
  r => DICT.has(r.key));

// ---- 태그 노이즈 패널 ----
document.getElementById("noisePanel").innerHTML = Object.entries(NOISE)
  .sort((a, b) => Object.values(b[1]["변형"]).reduce((x, y) => x + y, 0) -
                  Object.values(a[1]["변형"]).reduce((x, y) => x + y, 0))
  .map(([tag, d]) =>
    `<div class="noise-row"><span class="noise-tag">${esc(tag)} <span style="color:var(--muted)">${d["정식"]}</span></span>
     ${Object.entries(d["변형"]).map(([v, n]) =>
       `<span class="chip off">${esc(v)} ${n}</span>`).join("")}</div>`).join("")
  || '<div style="color:var(--muted)">변형 공존 없음</div>';

// ---- 필터 + 리스트 ----
const F = { q: "", region: "", tag: "", state: "", sort: "bloggers" };
const sel = id => document.getElementById(id);
[...new Set(CARDS.map(bucket))].sort().forEach(b =>
  sel("fRegion").insertAdjacentHTML("beforeend", `<option>${esc(b)}</option>`));
Object.entries(tagCount).sort((a, b) => b[1] - a[1]).forEach(([t]) =>
  sel("fTag").insertAdjacentHTML("beforeend", `<option>${esc(t)}</option>`));

function state(c){ return c.closed ? "closed" : (c["판정"] === "유지" ? "serving" : "hold"); }

function match(c){
  if (F.region && bucket(c) !== F.region) return false;
  if (F.tag && !(c.tags || []).includes(F.tag)) return false;
  if (F.state && state(c) !== F.state) return false;
  if (F.q){
    const hay = (c.name + " " + (c.aliases || []).join(" ") + " " +
      (c.summary || "") + " " + (c.tags || []).join(" ")).toLowerCase();
    if (!F.q.toLowerCase().split(/\s+/).every(w => hay.includes(w))) return false;
  }
  return true;
}
const SORT = {
  bloggers: (a, b) => (b.bloggers || 0) - (a.bloggers || 0),
  mention:  (a, b) => (b.mention_count || 0) - (a.mention_count || 0),
  rating:   (a, b) => (b.rating_avg || 0) - (a.rating_avg || 0),
  name:     (a, b) => a.name.localeCompare(b.name, "ko"),
};

function spotHTML(c){
  const st = state(c);
  const tags = (c.tags || []).map(t =>
    `<span class="chip${DICT.has(t) ? (HARD.has(t) ? " hard" : "") : " off"}">${esc(t)}</span>`).join("");
  const meta = [
    c.bloggers != null && `블로거 <b>${c.bloggers}</b>`,
    c.mention_count != null && `언급 <b>${c.mention_count}</b>`,
    c.rating_avg != null && `별점 <b>${c.rating_avg}</b> (${c.rating_count || 0})`,
    c.reaction_tone && `댓글톤 <b>${esc(c.reaction_tone)}</b>`,
    c.review_tone && `리뷰톤 <b>${esc(c.review_tone)}</b>`,
    c.hours_hint && esc(c.hours_hint),
    `place_id ${c.place_id ? "O" : "X"}`, `좌표 ${c.lat ? "O" : "X"}`,
    c.category && esc(c.category),
  ].filter(Boolean).join('</span><span>');
  return `<div class="spot">
    <div class="spot-head"><span class="spot-name">${esc(c.name)}</span>
      <span class="spot-region">${esc(c.region_fine || "")}${c.region_fine ? " · " : ""}${esc(bucket(c))}</span>
      ${st === "closed" ? '<span class="badge closed">폐업</span>' : ""}
      ${st === "hold" ? `<span class="badge hold">판정 ${esc(c["판정"] || "미정")}</span>` : ""}
      ${(c.aliases || []).length ? `<span style="font-size:12px;color:var(--muted)">별칭 ${c.aliases.length}</span>` : ""}
    </div>
    ${tags ? `<div class="spot-tags">${tags}</div>` : ""}
    ${c.summary ? `<div class="summary">${esc(c.summary)}</div>` : ""}
    ${(c.caution || []).length ? `<div class="caution">주의: ${esc(c.caution.join(", "))}</div>` : ""}
    <div class="meta"><span>${meta}</span></div>
    <details class="raw" data-name="${esc(c.name)}"><summary>원본 JSON</summary><pre></pre></details>
  </div>`;
}

// 원본 JSON은 펼칠 때만 생성 (831장 전부 미리 stringify하면 초기 렌더가 얼어붙음 — 실측)
const BY_NAME = {};
CARDS.forEach(c => BY_NAME[c.name] = c);
sel("list").addEventListener("toggle", e => {
  const d = e.target;
  if (d.classList?.contains("raw") && d.open && !d.querySelector("pre").textContent)
    d.querySelector("pre").textContent = JSON.stringify(BY_NAME[d.dataset.name], null, 2);
}, true);

const PAGE = 120;
let shown = PAGE;
function render(more){
  if (!more) shown = PAGE;
  const rows = CARDS.filter(match).sort(SORT[F.sort]);
  sel("count").textContent = `표시 ${Math.min(shown, rows.length)} / ${rows.length}장 (전체 ${CARDS.length})`;
  sel("list").innerHTML = rows.slice(0, shown).map(spotHTML).join("") +
    (rows.length > shown
      ? `<button id="more" style="width:100%;padding:10px;font:inherit;cursor:pointer;
           background:var(--surface);border:1px solid var(--axis);border-radius:8px;color:var(--ink)">
           더 보기 (${rows.length - shown}장 남음)</button>`
      : "") ||
    '<div style="color:var(--muted);padding:20px">조건에 맞는 카드 없음</div>';
  const btn = document.getElementById("more");
  if (btn) btn.onclick = () => { shown += PAGE; render(true); };
}
sel("q").oninput = e => { F.q = e.target.value; render(); };
sel("fRegion").onchange = e => { F.region = e.target.value; render(); };
sel("fTag").onchange = e => { F.tag = e.target.value; render(); };
sel("fState").onchange = e => { F.state = e.target.value; render(); };
sel("fSort").onchange = e => { F.sort = e.target.value; render(); };
sel("reset").onclick = () => { location.reload(); };
render();
</script>
</body>
</html>"""

out = (HTML
       .replace("__GENERATED__", generated)
       .replace("__TOTAL__", str(n_total))
       .replace("__SERVING__", str(n_serving))
       .replace("__HOLD__", str(n_hold))
       .replace("__CLOSED__", str(n_closed))
       .replace("__PID__", str(n_pid))
       .replace("__GEO__", str(n_geo))
       .replace("__CARDS__", json.dumps(cards, ensure_ascii=False, separators=(",", ":")))
       .replace("__DICT__", json.dumps(DICT_TAGS, ensure_ascii=False))
       .replace("__HARD__", json.dumps(HARD_TAGS, ensure_ascii=False))
       .replace("__NOISE__", json.dumps(noise, ensure_ascii=False)))

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write(out)
print("OK -> %s (%.1f MB, cards=%d, noise tags=%d)" %
      (OUT_PATH, len(out.encode("utf-8")) / 1e6, n_total, len(noise)))
