# -*- coding: utf-8 -*-
"""
검수 마스터 CSV — 유튜브(Pass 1) + 네이버(크롤) 신호를 카페 1행으로 통합.
전처리(필터) 적용:
  - 유효스니펫 = 카페명 포함 + postdate 2024-01 이후
  - 블로거수 = 유효스니펫의 고유 블로거
  - 플래그: 실존의심 / 이름불일치 / 비카페업종
출력: data/processed/review_master.csv (utf-8-sig) — 검수 CSV는 이거 하나만 유지.
"""
import csv
import json
import os
import re
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
RICH_ORDER = {"high": 0, "mid": 1, "low": 2}

TAG = re.compile(r"<[^>]+>")
def clean(s):
    return TAG.sub("", s or "").replace("&quot;", '"').replace("&amp;", "&").strip()

def norm(s):
    """비교용 정규화: 태그/괄호부/공백/기호 제거 + 소문자"""
    s = clean(s).split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())

# ---- 공공 registry: 실존 화이트리스트 ----
registry = json.load(open(os.path.join(ROOT, "data", "processed", "cafe_registry.json"), encoding="utf-8"))
regset = {norm(r["name"]) for r in registry if norm(r["name"])}

# ---- 유튜브(Pass 1) 신호: 카페명별 집계 ----
spots = json.load(open(os.path.join(ROOT, "data", "processed", "유튜브 정제.json"), encoding="utf-8"))
yt = {}
yt_mentions = defaultdict(int)
yt_maxview = defaultdict(int)
for s in spots:
    n = s["spot_name"]
    yt_mentions[n] += 1
    yt_maxview[n] = max(yt_maxview[n], s.get("view_count") or 0)
    # 대표 레코드: richness 최고 우선
    if n not in yt or RICH_ORDER.get(s.get("info_richness"), 9) < RICH_ORDER.get(yt[n].get("info_richness"), 9):
        yt[n] = s

# ---- 재검색(구제) 결과: spot_name → 정리된 이름으로 재크롤한 레코드 ----
rescue = {}
rescue_path = os.path.join(ROOT, "data", "raw", "네이버 재검색 크롤링.jsonl")
if os.path.exists(rescue_path):
    for line in open(rescue_path, encoding="utf-8", errors="replace"):
        try:
            rr = json.loads(line)
            if "blog" in rr:
                rescue[rr["spot_name"]] = rr
        except Exception:
            pass

# ---- 네이버 크롤 ----
rows = []
for line in open(os.path.join(ROOT, "data", "raw", "네이버 크롤링.jsonl"), encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    name = r["spot_name"]
    base = yt.get(name, {})
    key = norm(name)
    # 재검색 결과가 있으면 교체 (키도 정리된 이름 기준)
    if name in rescue:
        rr = rescue[name]
        r = {**r, "blog": rr["blog"], "local": rr.get("local", {})}
        key = norm(rr.get("cleaned_name") or name)

    items = r.get("blog", {}).get("items", [])
    valid = [it for it in items
             if key and key in norm(it.get("title", "") + it.get("description", ""))
             and it.get("postdate", "") >= "20240101"]
    bloggers = {it.get("bloggername") for it in valid}
    dates = sorted(it.get("postdate", "") for it in valid)

    loc = (r.get("local", {}).get("items") or [{}])[0]
    loc_title = clean(loc.get("title", ""))
    category = loc.get("category", "")

    # 플래그
    flags = []
    if len(valid) < 3 and not loc_title:
        flags.append("실존의심")
    if loc_title and key and key[:6] not in norm(loc_title):
        flags.append("이름불일치")
    if category and not any(w in category for w in ("카페", "커피", "디저트", "베이커리", "제과", "찻집", "브런치")):
        flags.append("비카페업종")

    # 판정: 상호/유효스니펫/registry 3신호 조합 (삭제는 사람이 확정)
    in_registry = key in regset
    if loc_title:
        verdict = "유지"
    elif valid or in_registry:
        verdict = "보류"   # 상호 없어도 다른 신호 있음 — 사람 검수
    else:
        verdict = "제외후보"  # 3신호 전부 없음 — 추출 노이즈 추정

    rows.append({
        "카페명": name,
        "판정": verdict,
        "registry매칭": "O" if in_registry else "",
        "지역": base.get("region") or r.get("region"),
        "정보등급": base.get("info_richness"),
        "유튜브_언급영상": yt_mentions[name],
        "유튜브_최고조회수": yt_maxview[name],
        "유튜브_요약": (base.get("summary") or "")[:80],
        "주소_Pass1": base.get("address") or "",
        "블로그_유효스니펫": len(valid),
        "블로그_블로거수": len(bloggers),
        "블로그_최신포스트": dates[-1] if dates else "",
        "블로그_총량참고": r.get("blog", {}).get("total", 0),
        "지역검색_상호": loc_title,
        "지역검색_업종": category,
        "지역검색_주소": loc.get("roadAddress") or loc.get("address", ""),
        "플래그": ";".join(flags),
        "스니펫예시": clean(valid[0].get("description", ""))[:100] if valid else "",
    })

# 재검색 후에도 3신호 전부 죽은 행은 작업표에서 제외 (raw는 보존 — 합의: 2026-07-08)
dropped = [r["카페명"] for r in rows if r["판정"] == "제외후보"]
rows = [r for r in rows if r["판정"] != "제외후보"]

# 정렬: 보류→유지 순으로 위에 (검수 대상부터), 그 안에서 블로거수 내림차순
VERDICT_ORDER = {"제외후보": 0, "보류": 1, "유지": 2}
rows.sort(key=lambda x: (VERDICT_ORDER.get(x["판정"], 9), -x["블로그_블로거수"]))

out = os.path.join(ROOT, "data", "processed", "review_master.csv")
with open(out, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

n_flag = sum(1 for r in rows if r["플래그"])
print(f"저장: {out} ({len(rows)}행, 플래그 {n_flag}건)")
from collections import Counter
print("판정 분포:", dict(Counter(r["판정"] for r in rows)))
print("플래그 분포:", dict(Counter(f for r in rows for f in r["플래그"].split(";") if f)))
print("registry 매칭:", sum(1 for r in rows if r["registry매칭"]), "곳")
print(f"제외(작업표에서만): {len(dropped)}곳 — raw 보존")
print("  제외 목록:", dropped[:20], "..." if len(dropped) > 20 else "")
