# -*- coding: utf-8 -*-
"""동료 hybrid_embedding_seed vs 현 서빙 코퍼스(smoke) 정면 비교 (작업용).

1) 시드 957곳 임베딩 → chroma_seed_test/ (1회, 있으면 스킵)
2) 같은 질문 → 양쪽 top-5
3) 자동 지표:
   - 지역 정합율: 지역 질의 top-5가 교정 지역과 맞는 비율 (주소 기반 SPOT_LOC 재사용)
   - 만능 자석 지수: 전체 질문 top-5에 3회+ 등장하는 문서
   - 이름 질의: '해지개' 류가 몇 위에 오는가 (이름 레이어 없이 순수 임베딩 비교)
결과는 _compare_seed_out.txt 에도 저장.
"""
import io
import json
import os
import sys
from collections import Counter

import chromadb
from openai import OpenAI

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.stdout = io.TextIOWrapper(open(os.path.join(ROOT, "_compare_seed_out.txt"), "wb"), encoding="utf-8")

env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8-sig"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
client = OpenAI(api_key=env["OPENAI_KEY"])

# ---- 1) 시드 적재 (이어달리기) ----
docs = [json.loads(l) for l in open(os.path.join(ROOT, "data", "rag", "hybrid_embedding_seed.jsonl"), encoding="utf-8")]
cdb = chromadb.PersistentClient(path=os.path.join(ROOT, "chroma_seed_test"))
try:
    col_seed = cdb.get_collection("seed_v2")
except Exception:
    col_seed = cdb.create_collection("seed_v2", metadata={"hnsw:space": "cosine"})
have = col_seed.count()
if have < len(docs):
    todo = docs[have:]
    print(f"[적재] {have} -> {len(docs)}")
    B = 100
    for i in range(0, len(todo), B):
        chunk = todo[i:i + B]
        embs = client.embeddings.create(model="text-embedding-3-large",
                                        input=[d["text"][:6000] for d in chunk])
        col_seed.add(ids=[d["id"] for d in chunk],
                     embeddings=[e.embedding for e in embs.data],
                     documents=[d["text"] for d in chunk],
                     metadatas=[{"cafe_name": d["metadata"]["cafe_name"]} for d in chunk])
        print(f"  +{i + len(chunk)}/{len(todo)}")
col_smoke = chromadb.PersistentClient(path=os.path.join(ROOT, "chroma_smoke")).get_collection("smoke")

# ---- 지역 정답지 (server의 주소 기반 교정 재사용) ----
sys.path.insert(0, ROOT)
from app.server import spot_bf, _label_to_bf  # noqa: E402  (server 임포트 부작용: 인덱스 로드)

QUERIES = [
    ("성산에서 오션뷰 보면서 커피 마시고 싶어", "성산"),
    ("조용히 책 읽기 좋은 카페", None),
    ("노을 맛집인 카페 알려줘", None),
    ("애월에서 강아지랑 같이 갈 수 있는 브런치 카페", "애월"),
    ("주차 편하고 자리 넓은 대형 카페", None),
    ("웨이팅 없이 여유롭게 있을 수 있는 로컬 카페", None),
    ("혼자 멍때리기 좋은 바닷가 카페", None),
    ("한림에서 빵 맛있는 베이커리 카페", "한림"),
    ("해지개", "NAME:해지개"),
    ("카페 오른", "NAME:오른"),
    ("월정리 카페", "월정리"),
]

def top5(col, q_emb, name_key):
    r = col.query(query_embeddings=[q_emb], n_results=8)
    out, seen = [], set()
    for m, dist in zip(r["metadatas"][0], r["distances"][0]):
        n = m.get("spot_name") or m.get("cafe_name")
        if n in seen:
            continue
        seen.add(n)
        out.append((n, 1 - dist))
        if len(out) == 5:
            break
    return out

magnet = {"smoke": Counter(), "seed": Counter()}
region_hits = {"smoke": [0, 0], "seed": [0, 0]}
name_rank = {"smoke": {}, "seed": {}}

for q, expect in QUERIES:
    q_emb = client.embeddings.create(model="text-embedding-3-large", input=[q]).data[0].embedding
    print(f"\n=== {q!r} (기대: {expect}) ===")
    for tag, col in (("smoke", col_smoke), ("seed", col_seed)):
        rows = top5(col, q_emb, tag)
        for n, s in rows:
            magnet[tag][n] += 1
        if expect and not expect.startswith("NAME:"):
            wb, wf = _label_to_bf(expect)
            for n, s in rows:
                b, f = spot_bf(n) or (None, None)
                ok = (f == wf and wf) or (b == wb)
                region_hits[tag][0] += 1 if ok else 0
                region_hits[tag][1] += 1
        if expect and expect.startswith("NAME:"):
            key = expect[5:]
            rank = next((i + 1 for i, (n, _) in enumerate(rows) if key in (n or "")), None)
            name_rank[tag][key] = rank
        line = " / ".join(f"{n}({s:.2f})" for n, s in rows)
        print(f"  [{tag:5}] {line}")

print("\n===== 자동 지표 =====")
for tag in ("smoke", "seed"):
    rh = region_hits[tag]
    print(f"[{tag}] 지역 정합: {rh[0]}/{rh[1]}"
          f" | 이름 질의 순위: {name_rank[tag]}"
          f" | 자석(3회+ 상위 노출): {[f'{n}x{c}' for n, c in magnet[tag].most_common(5) if c >= 3]}")
