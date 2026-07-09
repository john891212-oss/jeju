# -*- coding: utf-8 -*-
"""
[파이프라인 계약] 댓글 정제 자산 추출 — 반응 외 3자산(발굴·정보 슬롯·폐업)의 활용.

입력:  data/processed/댓글 정제.jsonl   (755편: A귀속 196 + B발굴 559)
       data/processed/review_master.csv (명단 조인)
출력:  data/processed/댓글부가.json     {spot_name: {addr_hint, hours_hint, phone_hint, parking_hint}}
                                        — 기존 명단 카페의 카드 보강 (표시층 전용)
       data/processed/발굴큐.jsonl      명단 밖 신규 카페 후보 (검증 대기 — 카카오/네이버 재크롤 대상)
       data/processed/폐업제보.json     closed_hint 참 목록 (merge.py 강등 재료)
소비자: app/server.py AUX / pipeline/merge.py / 발굴 검증 작업

원칙: 댓글 정보는 힌트 등급 (📍고정댓글 발이지만 검증 전) — 카드에 '댓글 발' 표기 권장.
"""
import csv
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def norm(s):
    return re.sub(r"[^\w가-힣]", "", (s or "").split("(")[0].lower())

recs = [json.loads(l) for l in open(os.path.join(ROOT, "data", "processed", "댓글 정제.jsonl"),
                                    encoding="utf-8")]
ours = {}
for r in csv.DictReader(open(os.path.join(ROOT, "data", "processed", "review_master.csv"),
                             encoding="utf-8-sig")):
    ours[r["카페명"]] = r["카페명"]
    ours.setdefault(norm(r["카페명"]), r["카페명"])

# 발굴 편입분 매핑: 댓글 속 이름 → 카카오 정식명 (ingest_discovered 이후 spot_name)
verified = {}
vp = os.path.join(ROOT, "data", "processed", "발굴검증.json")
if os.path.exists(vp):
    for c in json.load(open(vp, encoding="utf-8")).get("편입후보", []):
        verified[c["name"]] = c["kakao_name"]
        verified[norm(c["name"])] = c["kakao_name"]

aux, queue, closed = {}, [], []
for r in recs:
    name = r.get("spot_name") or r.get("cafe_identified") or ""
    if not name or name == "None":
        if r.get("closed_hint"):
            closed.append({"spot_name": None, "video_id": r["video_id"]})
        continue
    target = ours.get(name) or ours.get(norm(name)) or verified.get(name) or verified.get(norm(name))
    slots = r.get("info_slots") or {}
    if r.get("closed_hint"):
        closed.append({"spot_name": target or name, "video_id": r["video_id"],
                       "in_corpus": bool(target)})
    if target:
        e = aux.setdefault(target, {"addr_hint": "", "hours_hint": "", "phone_hint": "",
                                    "parking_hint": "", "reaction_tone": "", "reaction_hint": "",
                                    "video_ids": []})
        if slots.get("address") and not e["addr_hint"]:
            e["addr_hint"] = re.sub(r"[📍\s]+", " ", slots["address"]).strip()[:80]
        if slots.get("hours") and not e["hours_hint"]:
            e["hours_hint"] = slots["hours"][:80]
        etc = slots.get("etc") or ""
        m = re.search(r"(0\d{1,2}[- ]?\d{3,4}[- ]?\d{4})", etc)
        if m and not e["phone_hint"]:
            e["phone_hint"] = m.group(1)
        if "주차" in etc and not e["parking_hint"]:
            e["parking_hint"] = "주차 언급 있음(댓글)"
        # 반응(결정 20): 임베딩 금지 — 카드 표시·LLM 선별 입력용. 댓글 많은 영상 우선
        if r.get("reaction_summary") and (not e["reaction_hint"]
                                          or (r.get("n_comments", 0) or 0) > e.get("_rn", 0)):
            e["reaction_hint"] = r["reaction_summary"][:120]
            e["reaction_tone"] = r.get("reaction_tone", "")
            e["_rn"] = r.get("n_comments", 0) or 0
        if r.get("video_id") and r["video_id"] not in e["video_ids"]:
            e["video_ids"].append(r["video_id"])
    else:
        queue.append({"cafe_identified": name, "video_id": r["video_id"],
                      "address": slots.get("address"), "hours": slots.get("hours"),
                      "reaction_tone": r.get("reaction_tone"),
                      "reaction_summary": (r.get("reaction_summary") or "")[:100],
                      "n_comments": r.get("n_comments", 0)})

for e in aux.values():
    e.pop("_rn", None)
p1 = os.path.join(ROOT, "data", "processed", "댓글부가.json")
json.dump(aux, open(p1, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
p2 = os.path.join(ROOT, "data", "processed", "발굴큐.jsonl")
with open(p2, "w", encoding="utf-8") as f:
    for q in queue:
        f.write(json.dumps(q, ensure_ascii=False) + "\n")
p3 = os.path.join(ROOT, "data", "processed", "폐업제보.json")
json.dump(closed, open(p3, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

n_a = sum(1 for v in aux.values() if v["addr_hint"])
n_h = sum(1 for v in aux.values() if v["hours_hint"])
print(f"댓글부가: {len(aux)}카페 (주소 {n_a} / 영업시간 {n_h}) -> 댓글부가.json")
print(f"발굴큐(명단 밖 신규): {len(queue)}건 -> 발굴큐.jsonl")
print(f"폐업제보: {len(closed)}건 (명단 내 {sum(1 for c in closed if c.get('in_corpus'))}) -> 폐업제보.json")
