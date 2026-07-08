# -*- coding: utf-8 -*-
import json
import os
ROOT = os.path.dirname(os.path.abspath(__file__))
recs = [json.loads(l) for l in open(os.path.join(ROOT, "data", "processed", "네이버 정제.jsonl"), encoding="utf-8")]
print(f"총 {len(recs)}건\n")
for r in recs:
    print(f"■ {r['spot_name']} [{r['info_richness_blog']}] cat={r['category_hint']} closed={r['closed_hint']} snip={r['n_snippets_used']}/blogger {r['bloggers_used']}")
    print("  tags:", ", ".join(r["tags_blog"]) or "(없음)")
    print("  summary:", r["summary_blog"][:150] or "(빈 문자열)")
    print()
