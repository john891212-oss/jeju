# -*- coding: utf-8 -*-
"""브라우즈 모드 + LLM 선별·이유 검증 (작업용)"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.server import search, is_browse

print("== is_browse ==")
for q in ("애월 카페", "제주 카페 추천", "애월 오션뷰 카페", "해지개", "조용한 카페"):
    print(f"  {q!r} -> {is_browse(q)}")

for q, note in (("애월 카페", "브라우즈: 다수결 정렬 기대"),
                ("성산 오션뷰 카페", "조건: LLM 재정렬+이유"),
                ("해지개", "이름 조회 + 이유")):
    t0 = time.time()
    r = search(q, k=8)
    dt = time.time() - t0
    print(f"\n--- {q!r} [{note}] {dt:.1f}s browse={r['browse']} total={r['total']}")
    print(f"  intro: {r['intro'][:90]}")
    for c in r["cards"][:8]:
        pin = "PIN" if c.get("name_match") else "   "
        why = (c.get("reason") or "")[:55]
        print(f"  {pin} {c['score']:.3f} b{c['bloggers']:>3} {c['spot_name'][:16]:16} | {why}")
