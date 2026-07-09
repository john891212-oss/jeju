# -*- coding: utf-8 -*-
"""
[파이프라인 계약] 동료 시드 → 라벨 층 편입 (임베딩 금지, 표시·필터 전용).

입력:  data/rag/hybrid_embedding_seed.jsonl   동료(팀원) 하이브리드 시드 957곳
       data/processed/review_master.csv       우리 명단 (조인 대상)
출력:  data/processed/시드라벨.json            {spot_name: {tags_seed, caution, hours_hint}}
소비자: app/server.py AUX (카드 표시층) → 추후 pipeline/merge.py 상속

배경 (2026-07-09 실측): 시드 임베딩은 지역 정합 11/20으로 서빙 코퍼스(18/20)에 패,
  원인은 유튜브 소스 키워드 블록(지역 스팸). 반면 태그·주의 신호·영업시간 상세는
  라벨 층에서 자산 — "층을 옮기면 독이 약이 된다". 임베딩 편입 금지.
조인: 이름 (완전일치 → 정규화 일치). place_id 조인은 merge.py에서 업그레이드 예정.
"""
import csv
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def norm(s):
    s = (s or "").split("(")[0]
    s = re.sub(r"[^\w가-힣]", "", s.lower())
    for g in ("카페", "커피"):
        if len(s) > len(g) + 1:
            if s.startswith(g):
                s = s[len(g):]
            elif s.endswith(g):
                s = s[:-len(g)]
    return s

# 영업시간 힌트: 시드 텍스트의 블로그 요약 문장에서 절만 추출 (지어내기 아님 — 원문 절 그대로)
HOURS = re.compile(r"영업시간[은:]?\s*([^.。]*?(?:\d{1,2}:\d{2}|정휴|휴무)[^.。]*)")

seed = [json.loads(l) for l in open(os.path.join(ROOT, "data", "rag", "hybrid_embedding_seed.jsonl"),
                                    encoding="utf-8")]
ours = [r["카페명"] for r in csv.DictReader(
    open(os.path.join(ROOT, "data", "processed", "review_master.csv"), encoding="utf-8-sig"))]
by_exact = {n: n for n in ours}
by_norm = {}
for n in ours:
    by_norm.setdefault(norm(n), n)

out, hit_exact, hit_norm, miss = {}, 0, 0, 0
for d in seed:
    name = d["metadata"]["cafe_name"]
    target = by_exact.get(name)
    if target:
        hit_exact += 1
    else:
        target = by_norm.get(norm(name))
        if target:
            hit_norm += 1
        else:
            miss += 1
            continue
    m = d["metadata"]
    hours = HOURS.search(d.get("text") or "")
    rec = out.setdefault(target, {"tags_seed": [], "caution": [], "hours_hint": ""})
    rec["tags_seed"] = sorted(set(rec["tags_seed"]) | set(m.get("tags") or []))
    rec["caution"] = sorted(set(rec["caution"]) | set(m.get("caution") or []))
    if hours and not rec["hours_hint"]:
        rec["hours_hint"] = hours.group(1).strip()[:80]

path = os.path.join(ROOT, "data", "processed", "시드라벨.json")
json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
n_hours = sum(1 for v in out.values() if v["hours_hint"])
n_caution = sum(1 for v in out.values() if v["caution"])
print(f"조인: 완전 {hit_exact} + 정규화 {hit_norm} / 미조인 {miss}")
print(f"편입: {len(out)}카페 (영업시간 힌트 {n_hours}, 주의 신호 {n_caution}) -> {path}")
