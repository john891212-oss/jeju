# -*- coding: utf-8 -*-
"""
[파이프라인 계약] 임베딩 — 카드 정본(cards.json) → Chroma 적재 (+스모크 검증).

입력:  data/processed/cards.json      merge.py 산출 카드 정본 (병합·폐업 반영)
       data/processed/정본매핑.json    spot_name 변형 → canonical
       chroma_smoke/ 컬렉션 "smoke"   기존 hybrid 385 문서 (팀원 병합 풀 — 텍스트·벡터 재사용)
출력:  chroma_smoke/ 컬렉션 "cards" (text-embedding-3-large, cosine)
       ※ 기존 "smoke"는 보존 — 회귀 시 서버 컬렉션명만 되돌리면 복구 (2026-07-09 개편)
       ※ 정본 단위 1카페=1 blog 문서. 프릳츠 9조각 → 1문서 (중복은 여기서 죽는다)
키:    .env OPENAI_KEY
소비자: app/server.py (/search)

원칙: 검색은 유사도만 — 인기 수치(블로거수)는 임베딩에 안 넣음 (원칙 8)
      서빙 편입 = 판정 '유지' + 비폐업 (보류/제외/폐업 차단)
사용:
  python pipeline/embed.py           # 적재(있으면 스킵) + 스모크 8문항
  python pipeline/embed.py rebuild   # 재적재
"""
import json
import os
import sys

import chromadb
from openai import OpenAI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
client = OpenAI(api_key=env["OPENAI_KEY"])


def build():
    cards = json.load(open(os.path.join(ROOT, "data", "processed", "cards.json"), encoding="utf-8"))
    mapping = json.load(open(os.path.join(ROOT, "data", "processed", "정본매핑.json"), encoding="utf-8"))
    by_name = {c["name"]: c for c in cards}

    serving = {c["name"] for c in cards if c["판정"] == "유지" and not c["closed"]}
    blocked = {c["name"] for c in cards} - serving

    # ① blog 문서: 서빙 카드의 대표 요약 (정본당 1개 — 재임베딩 필요)
    new_docs = []   # (id, text, meta)
    for c in cards:
        if c["name"] in serving and (c["summary"] or "").strip():
            new_docs.append((f"blog::{c['name']}", c["summary"],
                             {"source": "blog", "spot_name": c["name"],
                              "region": c["region_fine"] or c["region_bucket"] or "기타"}))

    # ② hybrid 문서: 기존 smoke에서 이관 — 텍스트 불변이라 임베딩 벡터 재사용 (비용 0)
    cdb = chromadb.PersistentClient(path=os.path.join(ROOT, "chroma_smoke"))
    old = cdb.get_collection("smoke").get(include=["documents", "metadatas", "embeddings"])
    keep_docs, drop = [], []
    for i, m in enumerate(old["metadatas"]):
        if m.get("source") != "hybrid":
            continue
        canon = (mapping.get(m["spot_name"]) or {}).get("canonical") or m["spot_name"]
        if canon in blocked:
            drop.append(canon)
            continue
        meta = dict(m)
        meta["spot_name"] = canon
        card = by_name.get(canon)
        if card:
            meta["region"] = card["region_fine"] or card["region_bucket"] or meta.get("region") or "기타"
        keep_docs.append((f"hybrid::{canon}::{i}", old["documents"][i], meta, old["embeddings"][i]))
    only_hybrid = {d[2]["spot_name"] for d in keep_docs} - {c["name"] for c in cards}
    print(f"[embed] blog {len(new_docs)}문서(재임베딩) + hybrid {len(keep_docs)}문서(벡터 재사용)"
          f" / 폐업·비유지로 hybrid 제외 {len(drop)}건 / 카드 없는 hybrid-only 카페 {len(only_hybrid)}곳")
    return new_docs, keep_docs


def embed_texts(texts, batch=100):
    out = []
    for i in range(0, len(texts), batch):
        resp = client.embeddings.create(model="text-embedding-3-large", input=texts[i:i+batch])
        out.extend(d.embedding for d in resp.data)
        print(f"  임베딩 {min(i+batch, len(texts))}/{len(texts)}", flush=True)
    return out


SMOKE_QUERIES = [
    "성산에서 오션뷰 보면서 커피 마시고 싶어",
    "조용히 책 읽기 좋은 카페",
    "노을 맛집인 카페 알려줘",
    "애월에서 강아지랑 같이 갈 수 있는 브런치 카페",
    "주차 편하고 자리 넓은 대형 카페",
    "웨이팅 없이 여유롭게 있을 수 있는 로컬 카페",
    "소품샵 구경도 할 수 있는 감성 카페",
    "비 오는 날 가기 좋은 분위기 있는 카페",
]

if __name__ == "__main__":
    cdb = chromadb.PersistentClient(path=os.path.join(ROOT, "chroma_smoke"))
    if len(sys.argv) > 1 and sys.argv[1] == "rebuild":
        try:
            cdb.delete_collection("cards")
        except Exception:
            pass
    col = cdb.get_or_create_collection("cards", metadata={"hnsw:space": "cosine"})

    if col.count() == 0:
        new_docs, keep_docs = build()
        embs = embed_texts([d[1] for d in new_docs])
        docs = [(d[0], d[1], d[2], e) for d, e in zip(new_docs, embs)] + keep_docs
        for i in range(0, len(docs), 500):
            chunk = docs[i:i+500]
            col.add(ids=[d[0] for d in chunk],
                    documents=[d[1] for d in chunk],
                    metadatas=[d[2] for d in chunk],
                    embeddings=[d[3] for d in chunk])
        print(f"적재 완료: {col.count()}건 (컬렉션 'cards' — 'smoke'는 보존)")
    else:
        print(f"[스킵] 기존 컬렉션 {col.count()}건 (재적재는 'rebuild')")

    print("\n" + "=" * 70)
    for q in SMOKE_QUERIES:
        q_emb = client.embeddings.create(model="text-embedding-3-large", input=[q]).data[0].embedding
        res = col.query(query_embeddings=[q_emb], n_results=5)
        print(f"\n❓ {q}")
        for m, d, doc in zip(res["metadatas"][0], res["distances"][0], res["documents"][0]):
            print(f"    {1-d:.3f} [{m['source']}] {m['spot_name']} ({m['region']}) — {doc[:50]}")
