# -*- coding: utf-8 -*-
"""HOLD 삼심 결과(SAME)를 카카오플레이스.jsonl에 반영 (작업용)"""
import json, os, shutil
ROOT = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(ROOT, "data/processed/카카오플레이스.jsonl")
verdicts = {v["spot_name"]: v for v in json.load(open(os.path.join(ROOT, "data/processed/HOLD판정.json"), encoding="utf-8"))}
shutil.copy(P, P + ".bak2")
recs = [json.loads(l) for l in open(P, encoding="utf-8")]
n = 0
for r in recs:
    if r["status"] == "HOLD" and verdicts.get(r["spot_name"], {}).get("verdict") == "SAME":
        r["status"] = "MATCH_MANUAL"
        n += 1
with open(P, "w", encoding="utf-8") as f:
    for r in recs:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
from collections import Counter
c = Counter(r["status"] for r in recs)
got = sum(v for k, v in c.items() if k.startswith("MATCH"))
print(f"승격 {n} / 최종 {dict(c)} / place_id 확보 {got}/{len(recs)} ({got/len(recs)*100:.0f}%)")
