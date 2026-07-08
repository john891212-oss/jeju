# -*- coding: utf-8 -*-
"""이름 매치 레이어 검증: 해지개 1순위 + 의미 질의 회귀 (작업용, 커밋 제외 가능)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.server import search, name_lookup

print("== 사전 조회 단독 ==")
for q in ("해지개", "애월 해지개 카페", "조용한 카페", "성산 오션뷰"):
    print(f"  {q!r} -> {name_lookup(q)}")

print("== /search 통합 ==")
for q in ("해지개", "애월 해지개 카페", "성산 오션뷰 카페", "조용한 카페 혼자 책읽기", "노을 보면서 빵 먹기 좋은 곳"):
    r = search(q, k=5)
    print(f"--- {q!r} (region={r['region']}, relaxed={r['relaxed']})")
    for c in r["cards"]:
        pin = "PIN" if c.get("name_match") else "   "
        print(f"  {pin} {c['score']:.3f} {c['spot_name']} [{','.join(c['sources'])}] coord={'O' if c['lat'] else 'X'}")
