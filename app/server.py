# -*- coding: utf-8 -*-
"""
로컬 관통용 API 서버 — web(8503) ↔ 임베딩 검색(chroma_smoke) 다리.

실행:
  pip install fastapi uvicorn   (없으면)
  cd C:\\Users\\akals\\Documents\\GitHub\\jeju
  python -m uvicorn app.server:app --port 8000

엔드포인트:
  GET /search?q=조용한+카페&k=8
  → { query, region, cards: [ {spot_name, region, score, sources,
      summary_youtube, summary_blog, tags, video_ids, blog_links,
      mention_count, bloggers} ] }

설계 원칙 (기존 결정 계승):
  - 키는 서버에만 (.env) — 브라우저 노출 없음
  - 검색은 임베딩 유사도만, 인기 수치는 정렬 보조에 안 씀 (원칙 8)
  - 필터 완화 단계: 지역 필터 → 결과 부족 시 전체 (app/search.py mock과 동일)
"""
import json
import os
import re

import chromadb
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- .env ----
env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
client = OpenAI(api_key=env["OPENAI_KEY"])

cdb = chromadb.PersistentClient(path=os.path.join(ROOT, "chroma_smoke"))
col = cdb.get_collection("smoke")

# ---- 부가정보 인덱스 (시작 시 1회 로드) ----
RICH_ORDER = {"high": 0, "mid": 1, "low": 2}
TAG = re.compile(r"<[^>]+>")
def _clean(s):
    return TAG.sub("", s or "").strip()
def _norm(s):
    s = _clean(s).split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())

def _load_aux():
    aux = {}
    # 유튜브 정제: 대표 요약/태그/video_id/언급수
    spots = json.load(open(os.path.join(ROOT, "data", "processed", "유튜브 정제.json"), encoding="utf-8"))
    for s in spots:
        n = s["spot_name"]
        a = aux.setdefault(n, {"tags": set(), "video_ids": [], "mention_count": 0,
                               "summary_youtube": "", "summary_blog": "",
                               "blog_links": [], "bloggers": 0, "_rich": 9})
        a["mention_count"] += 1
        if s.get("video_id") and s["video_id"] not in a["video_ids"]:
            a["video_ids"].append(s["video_id"])
        a["tags"].update(s.get("tags") or [])
        r = RICH_ORDER.get(s.get("info_richness"), 9)
        if r < a["_rich"]:
            a["_rich"] = r
            a["summary_youtube"] = s.get("summary") or ""
    # 네이버 정제: 블로그 요약/태그/블로거수
    p = os.path.join(ROOT, "data", "processed", "네이버 정제.jsonl")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            a = aux.setdefault(r["spot_name"], {"tags": set(), "video_ids": [], "mention_count": 0,
                                                "summary_youtube": "", "summary_blog": "",
                                                "blog_links": [], "bloggers": 0, "_rich": 9})
            a["summary_blog"] = r.get("summary_blog") or ""
            a["tags"].update(r.get("tags_blog") or [])
            a["bloggers"] = r.get("bloggers_used", 0)
    # 블로그 링크: 캐시 있으면 사용, 없으면 크롤링 원본에서 1회 추출
    cache = os.path.join(ROOT, "data", "processed", "블로그링크.json")
    if os.path.exists(cache):
        links = json.load(open(cache, encoding="utf-8"))
    else:
        links = {}
        raw = os.path.join(ROOT, "data", "raw", "네이버 크롤링.jsonl")
        if os.path.exists(raw):
            for line in open(raw, encoding="utf-8", errors="replace"):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                key = _norm(rec["spot_name"])
                good = [it["link"] for it in rec.get("blog", {}).get("items", [])
                        if key and key in _norm(it.get("title", "") + it.get("description", ""))
                        and it.get("postdate", "") >= "20240101"][:3]
                if good:
                    links[rec["spot_name"]] = good
            json.dump(links, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    for n, ls in links.items():
        if n in aux:
            aux[n]["blog_links"] = ls
    return aux

AUX = _load_aux()
print(f"[server] 부가정보 {len(AUX)}카페 로드 완료")

REGIONS = ["애월", "곽지", "한림", "협재", "함덕", "월정리", "세화", "김녕", "성산",
           "표선", "남원", "위미", "중문", "사계", "대정", "안덕", "우도", "구좌", "조천",
           "제주시내", "서귀포시내"]
ALIAS = {"서귀포": "서귀포시내", "제주시": "제주시내", "월정": "월정리"}

def detect_region(q):
    for r in REGIONS:
        if r in q:
            return r
    for a, std in ALIAS.items():
        if a in q:
            return std
    return None

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/search")
def search(q: str, k: int = 8):
    region = detect_region(q)
    q_emb = client.embeddings.create(model="text-embedding-3-large", input=[q]).data[0].embedding

    def run(where):
        return col.query(query_embeddings=[q_emb], n_results=k * 3,
                         where=where) if where else col.query(query_embeddings=[q_emb], n_results=k * 3)

    res = run({"region": region} if region else None)
    # 지역 필터가 너무 좁으면 전체로 완화 (코드 폴백 — LLM 루프 아님)
    relaxed = False
    if region and len(res["ids"][0]) < k:
        res = run(None)
        relaxed = True

    # spot 단위 병합: 같은 카페의 youtube/blog 문서 중 최고 점수
    spots = {}
    for meta, dist in zip(res["metadatas"][0], res["distances"][0]):
        n = meta["spot_name"]
        score = 1 - dist
        s = spots.setdefault(n, {"spot_name": n, "region": meta.get("region"),
                                 "score": 0.0, "sources": []})
        s["score"] = max(s["score"], score)
        if meta["source"] not in s["sources"]:
            s["sources"].append(meta["source"])

    cards = []
    for n, s in sorted(spots.items(), key=lambda x: -x[1]["score"])[:k]:
        a = AUX.get(n, {})
        cards.append({**s,
                      "score": round(s["score"], 3),
                      "summary_youtube": a.get("summary_youtube", ""),
                      "summary_blog": a.get("summary_blog", ""),
                      "tags": sorted(a.get("tags", [])),
                      "video_ids": a.get("video_ids", [])[:3],
                      "blog_links": a.get("blog_links", []),
                      "mention_count": a.get("mention_count", 0),
                      "bloggers": a.get("bloggers", 0)})
    return {"query": q, "region": region, "relaxed": relaxed, "cards": cards}

@app.get("/health")
def health():
    return {"ok": True, "docs": col.count(), "cafes": len(AUX)}
