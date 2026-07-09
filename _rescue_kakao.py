# -*- coding: utf-8 -*-
"""카카오플레이스 후처리 (작업용):
① HOLD 승격 — 좌표 100m 미만 + 이름 문자 유사도 0.5+ → MATCH_ALIAS (표기 변형 동일 가게)
② MISS 구제 — 판정 유지만, 이름 정리 변형으로 재검색 (네이버 재검색 56% 회생 패턴 재사용)
원본은 .bak 백업 후 갱신."""
import json, os, re, shutil, time, difflib, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
import sys
sys.path.insert(0, ROOT)
from pipeline.kakao_place import kakao, pick, core, norm, name_ok

PATH = os.path.join(ROOT, "data", "processed", "카카오플레이스.jsonl")
shutil.copy(PATH, PATH + ".bak")
recs = [json.loads(l) for l in open(PATH, encoding="utf-8")]

# ① HOLD 승격
promoted = 0
for r in recs:
    if r["status"] == "HOLD" and r.get("dist_m") is not None and r["dist_m"] < 100:
        sim = difflib.SequenceMatcher(None, core(r["spot_name"]) or norm(r["spot_name"]),
                                      core(r.get("kakao_name", "")) or norm(r.get("kakao_name", ""))).ratio()
        if sim >= 0.5:
            r["status"] = "MATCH_ALIAS"
            r["alias_sim"] = round(sim, 2)
            promoted += 1
print(f"① HOLD 승격: {promoted}")

# ② MISS 구제 (판정 유지만)
def variants(name):
    out = []
    base = name.split("(")[0].strip()
    c = core(name)
    if c and c != norm(base):
        out.append(c)
    toks = base.split()
    if len(toks) > 1 and len(toks[0]) >= 3:
        out.append(toks[0])
    runs = re.findall(r"[가-힣]{3,}", base)
    for run in runs[:2]:
        if run not in out and run != base:
            out.append(run)
    return out[:3]

import csv
extra = json.load(open(os.path.join(ROOT, "data", "processed", "카페부가v2.json"), encoding="utf-8"))
rescued = tried = 0
for r in recs:
    if r["status"] != "MISS" or r.get("판정") != "유지":
        continue
    name = r["spot_name"]
    e = extra.get(name) or {}
    nlat, nlng = e.get("lat"), e.get("lng")
    for v in variants(name):
        docs = kakao(v + " 제주")
        tried += 1
        if not docs:
            continue
        # 변형 기준 매칭: 좌표 있으면 근접 필수, 없으면 카카오명이 변형을 포함해야
        best, ok = None, False
        for d in docs:
            n_ok = name_ok(v, d["place_name"])
            if nlat and nlng:
                try:
                    from pipeline.kakao_place import dist_m as _dm
                    dd = _dm(nlat, nlng, float(d["y"]), float(d["x"]))
                except ValueError:
                    continue
                if n_ok and dd < 500:
                    best, ok = (d, dd), True
                    break
            elif n_ok and len(norm(v)) >= 3:
                best, ok = (d, None), True
                break
        if ok:
            d, dd = best
            r.update({"status": "MATCH_RESCUE", "rescue_query": v,
                      "place_id": d["id"], "kakao_name": d["place_name"],
                      "road_address": d.get("road_address_name") or "",
                      "address": d.get("address_name") or "",
                      "lat": float(d["y"]), "lng": float(d["x"]),
                      "phone": d.get("phone") or "", "category": d.get("category_name") or "",
                      "place_url": d.get("place_url") or "",
                      "dist_m": round(dd) if dd is not None else None})
            rescued += 1
            break
        time.sleep(0.05)
print(f"② MISS 구제: {rescued} (재검색 {tried}콜)")

with open(PATH, "w", encoding="utf-8") as f:
    for r in recs:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

from collections import Counter
c = Counter(r["status"] for r in recs)
n = len(recs)
got = c["MATCH"] + c["MATCH_NOCOORD"] + c["MATCH_ALIAS"] + c["MATCH_RESCUE"]
print(f"최종: {dict(c)}")
print(f"place_id 확보 {got}/{n} ({got/n*100:.0f}%)")
