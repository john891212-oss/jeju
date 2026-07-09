# -*- coding: utf-8 -*-
"""발굴큐 검증 (작업용): 카카오 조회로 실존+카테고리 확인 → 편입 후보/기각 분류"""
import json, os, sys, time
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from pipeline.kakao_place import kakao, name_ok

q = [json.loads(l) for l in open(os.path.join(ROOT, "data/processed/발굴큐.jsonl"), encoding="utf-8")]
names = sorted(set(x["cafe_identified"] for x in q))
CAFE_CAT = ("카페", "디저트", "베이커리", "찻집", "제과")
ok_list, rej = [], []
for n in names:
    if len(n) < 2 or n.startswith("("):
        rej.append((n, "서술구"))
        continue
    docs = kakao(n + " 제주") or []
    hit = None
    for d in docs:
        if name_ok(n, d["place_name"]) and "제주" in (d.get("address_name") or ""):
            hit = d
            break
    if not hit:
        rej.append((n, "카카오 미확인"))
    elif any(c in (hit.get("category_name") or "") for c in CAFE_CAT):
        ok_list.append({"name": n, "kakao_name": hit["place_name"], "place_id": hit["id"],
                        "category": hit["category_name"], "address": hit.get("road_address_name") or hit.get("address_name")})
    else:
        rej.append((n, "비카페: " + (hit.get("category_name") or "?")[:30]))
    time.sleep(0.06)

out = os.path.join(ROOT, "data/processed/발굴검증.json")
json.dump({"편입후보": ok_list, "기각": rej}, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"편입 후보 {len(ok_list)} / 기각 {len(rej)} -> 발굴검증.json")
for c in ok_list:
    print("  ✓", c["name"], "->", c["kakao_name"], "|", c["category"].split(">")[-1])
for n, why in rej[:15]:
    print("  ✗", n, "|", why)
