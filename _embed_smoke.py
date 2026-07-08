# -*- coding: utf-8 -*-
"""
임베딩 스모크 — 유튜브 정제본 vs 블로그 정제본을 같은 질문으로 나란히 비교.

- 문서 A: 카페-변환.json summary (high/mid, 빈 값 제외)  → source=youtube
- 문서 B: 카페-블로그정제.jsonl summary_blog (빈 값·closed 제외) → source=blog
- 임베딩: text-embedding-3-large / 저장: chroma_smoke/ (프로덕션 chroma_db와 분리)
- 검증: 스모크 질문 8개 × 출처별 top-5 비교 출력

사용:
  python _embed_smoke.py           # 적재(이미 있으면 스킵) + 질문 비교
  python _embed_smoke.py rebuild   # 컬렉션 지우고 재적재
"""
import json
import os
import sys

import chromadb
from openai import OpenAI

ROOT = os.path.dirname(os.path.abspath(__file__))
RICH_ORDER = {"high": 0, "mid": 1, "low": 2}

env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
client = OpenAI(api_key=env["OPENAI_KEY"])

# ---- 문서 구성 ----
def build_docs():
    docs = []  # (id, text, meta)

    # A. 유튜브: 고유 카페 대표 레코드 (richness 최고), summary 있는 것만
    spots = json.load(open(os.path.join(ROOT, "data", "processed", "유튜브 정제.json"), encoding="utf-8"))
    best = {}
    for s in spots:
        n = s["spot_name"]
        if n not in best or RICH_ORDER.get(s.get("info_richness"), 9) < RICH_ORDER.get(best[n].get("info_richness"), 9):
            best[n] = s
    for n, s in best.items():
        if (s.get("summary") or "").strip() and s.get("info_richness") in ("high", "mid"):
            docs.append((f"yt::{n}", s["summary"],
                         {"source": "youtube", "spot_name": n,
                          "region": s.get("region") or "기타",
                          "richness": s.get("info_richness")}))

    # B. 블로그: summary_blog 있는 것만, 폐업 제외
    path = os.path.join(ROOT, "data", "processed", "네이버 정제.jsonl")
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if not (r.get("summary_blog") or "").strip() or r.get("closed_hint"):
            continue
        n = r["spot_name"]
        meta_src = best.get(n, {})
        docs.append((f"blog::{n}", r["summary_blog"],
                     {"source": "blog", "spot_name": n,
                      "region": meta_src.get("region") or "기타",
                      "richness": r.get("info_richness_blog") or ""}))
    return docs

def embed_texts(texts, batch=100):
    out = []
    for i in range(0, len(texts), batch):
        resp = client.embeddings.create(model="text-embedding-3-large", input=texts[i:i+batch])
        out.extend(d.embedding for d in resp.data)
        print(f"  임베딩 {min(i+batch, len(texts))}/{len(texts)}", flush=True)
    return out

# ---- 적재 ----
cdb = chromadb.PersistentClient(path=os.path.join(ROOT, "chroma_smoke"))
if len(sys.argv) > 1 and sys.argv[1] == "rebuild":
    try:
        cdb.delete_collection("smoke")
    except Exception:
        pass
col = cdb.get_or_create_collection("smoke", metadata={"hnsw:space": "cosine"})

if col.count() == 0:
    docs = build_docs()
    n_yt = sum(1 for d in docs if d[2]["source"] == "youtube")
    print(f"적재: 총 {len(docs)}문서 (유튜브 {n_yt} / 블로그 {len(docs)-n_yt})")
    embs = embed_texts([d[1] for d in docs])
    for i in range(0, len(docs), 500):
        chunk = docs[i:i+500]
        col.add(ids=[d[0] for d in chunk],
                documents=[d[1] for d in chunk],
                metadatas=[d[2] for d in chunk],
                embeddings=embs[i:i+500])
    print(f"chroma_smoke/ 적재 완료: {col.count()}건")
else:
    print(f"[스킵] 기존 컬렉션 {col.count()}건 사용 (재적재는 'rebuild' 인자)")

# ---- 스모크 질문: 출처별 나란히 비교 ----
QUERIES = [
    "성산에서 오션뷰 보면서 커피 마시고 싶어",
    "조용히 책 읽기 좋은 카페",
    "노을 맛집인 카페 알려줘",
    "애월에서 강아지랑 같이 갈 수 있는 브런치 카페",
    "주차 편하고 자리 넓은 대형 카페",
    "웨이팅 없이 여유롭게 있을 수 있는 로컬 카페",
    "소품샵 구경도 할 수 있는 감성 카페",
    "비 오는 날 가기 좋은 분위기 있는 카페",
]

print("\n" + "=" * 70)
for q in QUERIES:
    q_emb = client.embeddings.create(model="text-embedding-3-large", input=[q]).data[0].embedding
    print(f"\n❓ {q}")
    for src in ("youtube", "blog"):
        res = col.query(query_embeddings=[q_emb], n_results=5, where={"source": src})
        names = res["metadatas"][0]
        dists = res["distances"][0]
        print(f"  [{src}]")
        for m, d, doc in zip(names, dists, res["documents"][0]):
            print(f"    {1-d:.3f} {m['spot_name']} ({m['region']}) — {doc[:50]}")
print("\n[스모크 완료 — chroma_smoke/는 실험용, 프로덕션 아님]")
