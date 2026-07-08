# -*- coding: utf-8 -*-
"""네이버 정제 결과 통계 (읽기 전용)."""
import json
import os
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
recs = [json.loads(l) for l in open(os.path.join(ROOT, "data", "processed", "네이버 정제.jsonl"), encoding="utf-8") if l.strip()]

print(f"총 {len(recs)}건")
print("richness:", dict(Counter(r["info_richness_blog"] for r in recs)))
print("빈 summary:", sum(1 for r in recs if not (r.get("summary_blog") or "").strip()), "건")
closed = [r["spot_name"] for r in recs if r.get("closed_hint")]
print(f"closed_hint=True: {len(closed)}건 → {closed}")
cats = Counter(r.get("category_hint") for r in recs)
print("category_hint:", dict(cats.most_common()))
extra = Counter(t for r in recs for t in r.get("tags_extra", []))
print("\ntags_extra 상위 20 (사전 승격 후보):")
for t, n in extra.most_common(20):
    print(f"  {t}: {n}")
