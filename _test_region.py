# -*- coding: utf-8 -*-
"""트랙 1 검증: 주소 기반 region 교정 + 계층 필터 + 이름 레이어 회귀 (작업용)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.server import search, addr_to_region, spot_bf

print("== addr_to_region 단위 ==")
for a in ("제주특별자치도 서귀포시 성산읍 해맞이해안로 2714",
          "제주특별자치도 제주시 애월읍 애월북서길 32",
          "제주특별자치도 제주시 한림읍 협재리 1732-3",
          "제주특별자치도 서귀포시 중문동 1234",
          "제주특별자치도 제주시 서문로 7-1",
          "제주특별자치도 제주시 한경면 저지리 1",
          ""):
    print(f"  {a[:28]!r:32} -> {addr_to_region(a)}")

print("== 교정 확인 (오염 실측 사례) ==")
for n in ("오른", "레이지펌프", "베케", "카이막", "고사리커피"):
    print(f"  {n}: {spot_bf(n)}")

print("== /search 통합 ==")
for q in ("성산 오션뷰 카페", "한림 바다 보이는 카페", "협재 감성 카페", "해지개",
          "조용한 카페 혼자 책읽기"):
    r = search(q, k=6)
    print(f"--- {q!r} (region={r['region']}, relaxed={r['relaxed']})")
    for c in r["cards"]:
        pin = "PIN" if c.get("name_match") else "   "
        print(f"  {pin} {c['score']:.3f} {c['spot_name']} [{c['region_bucket']}/{c['region_fine']}]")
