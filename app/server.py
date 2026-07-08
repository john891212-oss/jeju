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
import urllib.request
import urllib.error

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
GKEY = env.get("API_KEY")  # 유튜브=구글 클라우드 키. Places 사진도 이 키로 (서버에서만, 브라우저 노출 없음)

cdb = chromadb.PersistentClient(path=os.environ.get("CHROMA_DIR", os.path.join(ROOT, "chroma_smoke")))
col = cdb.get_collection("smoke")

# ---- 부가정보 인덱스 (시작 시 1회 로드) ----
RICH_ORDER = {"high": 0, "mid": 1, "low": 2}
TAG = re.compile(r"<[^>]+>")
def _clean(s):
    return TAG.sub("", s or "").strip()
def _norm(s):
    s = _clean(s).split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())

def _blank():
    return {"tags": set(), "video_ids": [], "mention_count": 0,
            "summary_youtube": "", "summary_blog": "",
            "blog_links": [], "bloggers": 0, "_rich": 9,
            "lat": None, "lng": None}

def _load_aux():
    aux = {}
    # 유튜브 정제: 대표 요약/태그/video_id/언급수
    spots = json.load(open(os.path.join(ROOT, "data", "processed", "유튜브 정제.json"), encoding="utf-8"))
    for s in spots:
        n = s["spot_name"]
        a = aux.setdefault(n, _blank())
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
            a = aux.setdefault(r["spot_name"], _blank())
            a["summary_blog"] = r.get("summary_blog") or ""
            a["tags"].update(r.get("tags_blog") or [])
            a["bloggers"] = r.get("bloggers_used", 0)
    # 블로그 링크 + 지역검색 좌표: 캐시(v2) 있으면 사용, 없으면 크롤링 원본에서 1회 추출
    cache = os.path.join(ROOT, "data", "processed", "카페부가v2.json")
    if os.path.exists(cache):
        extra = json.load(open(cache, encoding="utf-8"))
    else:
        extra = {}
        for raw_name in ("네이버 크롤링.jsonl", "네이버 재검색 크롤링.jsonl"):
            raw = os.path.join(ROOT, "data", "raw", raw_name)
            if not os.path.exists(raw):
                continue
            for line in open(raw, encoding="utf-8", errors="replace"):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                key = _norm(rec.get("cleaned_name") or rec["spot_name"])
                good = [it["link"] for it in rec.get("blog", {}).get("items", [])
                        if key and key in _norm(it.get("title", "") + it.get("description", ""))
                        and it.get("postdate", "") >= "20240101"][:3]
                e = extra.setdefault(rec["spot_name"], {"links": [], "lat": None, "lng": None})
                if good and not e["links"]:
                    e["links"] = good
                loc = (rec.get("local", {}).get("items") or [{}])[0]
                if loc.get("mapx") and loc.get("mapy") and e["lat"] is None:
                    try:  # 네이버 mapx/mapy = WGS84 * 1e7
                        e["lng"] = int(loc["mapx"]) / 1e7
                        e["lat"] = int(loc["mapy"]) / 1e7
                    except ValueError:
                        pass
        json.dump(extra, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    for n, e in extra.items():
        if n in aux:
            aux[n]["blog_links"] = e.get("links") or []
            aux[n]["lat"], aux[n]["lng"] = e.get("lat"), e.get("lng")
    # 좌표는 네이버 지역검색만 사용 (registry 좌표는 신뢰성 문제로 미사용 — 민옥 결정 2026-07-08)
    return aux

AUX = _load_aux()
print(f"[server] 부가정보 {len(AUX)}카페 로드 완료")

# ---- 이름 매치 레이어 (결정적 조회) ----
# 배경: 고유명사 조회는 임베딩의 직업이 아님 — "해지개" 검색 시 top10 전멸 실측 (2026-07-08).
#       임베딩 문서엔 카페명이 없고, 있어도 희귀 고유명사는 유사도가 안 잡힘.
# 원칙: 질의에 서빙 코퍼스의 카페명이 포함되면 해당 카드를 무조건 1순위 고정 (코드가 강제).
#       임베딩은 나머지 슬롯만 채움. 사전은 chroma(=판정 유지·폐업 제외 통과분)에서 구축.
_NAME_STOP = {"카페", "커피", "제주", "제주도", "베이커리", "디저트", "브런치",
              "애월", "곽지", "한림", "협재", "함덕", "월정리", "세화", "김녕", "성산",
              "표선", "남원", "위미", "중문", "사계", "대정", "안덕", "우도", "구좌", "조천",
              "제주시내", "서귀포시내", "서귀포", "제주시", "월정"}  # 일반어·지역명은 이름 아님

def _build_name_index():
    idx = {}  # 정규화 이름 → {name, region, sources}
    meta = col.get(include=["metadatas"])["metadatas"]
    for m in meta:
        n = m.get("spot_name")
        if not n:
            continue
        key = _norm(n)
        if len(key) < 2 or key in _NAME_STOP:
            continue
        e = idx.setdefault(key, {"name": n, "region": m.get("region"), "sources": []})
        if m.get("source") and m["source"] not in e["sources"]:
            e["sources"].append(m["source"])
    return idx

def name_lookup(q, limit=2):
    """질의 문자열에서 카페명 탐지. 긴 이름 우선, 최대 limit곳.
    len>=3은 부분 포함 허용, len==2는 완전 일치만 (오탐 방지)."""
    qn = _norm(q)
    hits = []
    for key, e in NAME_IDX.items():
        if (len(key) >= 3 and key in qn) or key == qn:
            hits.append((len(key), e))
    hits.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, e in hits:
        if e["name"] not in seen:
            seen.add(e["name"])
            out.append(e)
        if len(out) >= limit:
            break
    return out

NAME_IDX = _build_name_index()
print(f"[server] 이름 사전 {len(NAME_IDX)}건 구축 완료")

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


def _places_photo_uris(name, lat, lng, place_id=None, limit=8):
    """Places(New)로 카페 사진의 임시 URL 목록을 반환. place_id 있으면 Details, 없으면 이름+좌표 Text Search.
    사진 id(photo name)는 저장 금지·만료 대상이라 매 호출마다 신선하게 얻어 임시 URL(photoUri)로 변환한다."""
    if place_id:
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places/" + place_id,
            headers={"X-Goog-Api-Key": GKEY, "X-Goog-FieldMask": "id,photos"})
        d = json.load(urllib.request.urlopen(req, timeout=12))
        pid, photos = d.get("id"), d.get("photos", [])
    else:
        payload = {"textQuery": (name or "") + " 제주 카페", "languageCode": "ko", "maxResultCount": 1}
        if lat and lng:
            payload["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": 600.0}}
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": GKEY,
                     "X-Goog-FieldMask": "places.id,places.photos"})
        d = json.load(urllib.request.urlopen(req, timeout=12))
        places = d.get("places", [])
        if not places:
            return None, []
        pid, photos = places[0].get("id"), places[0].get("photos", [])
    uris = []
    for ph in photos[:limit]:
        murl = ("https://places.googleapis.com/v1/" + ph["name"] +
                "/media?maxWidthPx=800&skipHttpRedirect=true&key=" + GKEY)
        try:
            md = json.load(urllib.request.urlopen(murl, timeout=12))
            if md.get("photoUri"):
                uris.append(md["photoUri"])
        except Exception:
            pass
    return pid, uris


app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/search")
def search(q: str, k: int = 8):
    region = detect_region(q)
    pinned = name_lookup(q)  # 이름 조회는 임베딩보다 먼저, 결정적으로
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

    # 이름 매치 고정: 질의에 카페명이 있으면 그 카드가 무조건 앞 (임베딩 점수 무관)
    ordered = []
    for e in pinned:
        p = spots.pop(e["name"], None) or {"spot_name": e["name"], "region": e["region"],
                                           "score": 1.0, "sources": e["sources"]}
        p["name_match"] = True
        ordered.append(p)
    ordered += sorted(spots.values(), key=lambda s: -s["score"])
    ordered = ordered[:k]

    cards = []
    for s in ordered:
        n = s["spot_name"]
        a = AUX.get(n, {})
        cards.append({**s,
                      "score": round(s["score"], 3),
                      "summary_youtube": a.get("summary_youtube", ""),
                      "summary_blog": a.get("summary_blog", ""),
                      "tags": sorted(a.get("tags", [])),
                      "video_ids": a.get("video_ids", [])[:3],
                      "blog_links": a.get("blog_links", []),
                      "mention_count": a.get("mention_count", 0),
                      "bloggers": a.get("bloggers", 0),
                      "lat": a.get("lat"), "lng": a.get("lng")})
    return {"query": q, "region": region, "relaxed": relaxed, "cards": cards}

@app.get("/photos")
def photos(name: str, lat: float = None, lng: float = None, place_id: str = None):
    """카페 구글 사진의 임시 URL 목록. 키는 서버에만 — 프론트엔 URL만 나감."""
    if not GKEY:
        return {"error": "no_api_key", "photos": []}
    try:
        pid, uris = _places_photo_uris(name, lat, lng, place_id)
        return {"name": name, "place_id": pid, "photos": uris}
    except urllib.error.HTTPError as e:
        return {"error": "google_%d" % e.code,
                "detail": e.read().decode("utf-8", "replace")[:200], "photos": []}
    except Exception as e:
        return {"error": type(e).__name__, "photos": []}


@app.get("/health")
def health():
    return {"ok": True, "docs": col.count(), "cafes": len(AUX)}
