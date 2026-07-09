# -*- coding: utf-8 -*-
"""근거 API 검증 (작업용)"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.server import _evidence_impl

for name, q in (("책계일주", "조용한 카페 책읽기"), ("해지개", "노을 보면서 빵"), ("카페 오른", "오션뷰")):
    t0 = time.time()
    r = _evidence_impl(name, q)
    dt = time.time() - t0
    print(f"--- {name} x {q!r} ({dt:.1f}s): 스니펫 {r['n_snippets']} / 매칭 {r['n_matched']} / 블로거 {r['bloggers_matched']}")
    print("  의견:", r["opinion"][:110])
    for qt in r["quotes"]:
        print("  인용:", qt[:75])
    for y in r["youtube_reactions"]:
        print("  영상반응:", y["tone"], "|", y["summary"][:65])
