# -*- coding: utf-8 -*-
"""반응 연결 검증 (작업용): 신규 카페 카드에 영상·반응 붙었는지 + LLM이 반응을 읽는지"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.server import search, AUX

print("== 신규 카페 AUX 확인 ==")
for n in ("떼르떼", "안티코", "제주당"):
    a = AUX.get(n)
    if a:
        print(f"  {n}: 영상 {len(a['video_ids'])} / 톤 {a['reaction_tone']!r} / 반응 {a['reaction_hint'][:50]!r} / 시간 {a['hours_hint'][:30]!r}")
    else:
        print(f"  {n}: AUX 없음")

print("== 검색 통합 (제주당 — 혼합 여론 카페) ==")
r = search("애월 빵 맛있는 대형 카페", k=6)
for c in r["cards"]:
    print(f"  {c['spot_name'][:14]:14} 톤={c['reaction_tone'] or '-':3} | {(c.get('reason') or '')[:60]}")
