# -*- coding: utf-8 -*-
"""
로컬 관통용 API 서버 — web(8503) ↔ 카드 정본 검색 다리.

실행:
  pip install fastapi uvicorn   (없으면)
  cd C:\\Users\\akals\\Documents\\GitHub\\jeju
  python -m uvicorn app.server:app --port 8000

엔드포인트:
  GET /search?q=조용한+카페&k=8   → { query, region, browse, total, cards: [...] }
  GET /evidence?name=프릳츠&q=조용  → 원문 근거 (블로그 인용 + 리뷰 + 쇼츠 반응)
  GET /photos?name=...             → 구글 사진 임시 URL (키는 서버에만)
  GET /health

설계 원칙 (2026-07-09 개편 — 런타임 LLM 철거):
  - 카드 정본(cards.json, merge.py 산출)이 유일한 카페 사전 — 중복은 병합이 이미 죽였다
  - 런타임 생성 LLM 없음. "왜 추천"은 코드가 결정적으로 조립(matched: 지역·태그·블로거)
    LLM의 자리는 오프라인 파이프라인(정제·태그·요약)이지 매 검색의 톨게이트가 아니다
  - 임베딩(text-embedding-3-large)은 유지 — 태그 사전 밖 자연어 조건의 폴백 (결정적·빠름)
  - 근거는 원문 인용만: /evidence quotes = 매칭 스니펫 코드 발췌 (지어내기 원천 차단)
  - 폐업은 지우지 않고 안내: 이름 조회에는 closed 카드로 응답, 검색·브라우즈에선 제외
  - 키는 서버에만 (.env) — 브라우저 노출 없음
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
client = OpenAI(api_key=env["OPENAI_KEY"])  # 임베딩 전용 (생성 호출 없음)
GKEY = env.get("API_KEY")  # 유튜브=구글 클라우드 키. Places 사진도 이 키로

cdb = chromadb.PersistentClient(path=os.environ.get("CHROMA_DIR", os.path.join(ROOT, "chroma_smoke")))
col = cdb.get_collection(os.environ.get("CHROMA_COLLECTION", "cards"))  # 회귀 시 "smoke"로 복구

TAG = re.compile(r"<[^>]+>")
def _clean(s):
    return TAG.sub("", s or "").strip()
def _norm(s):
    s = _clean(s).split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())

# ---- 카드 정본 로드 (merge.py 산출 — 병합·지역교정·폐업 강등 완료) ----
CARDS = {}      # 정본명 → 카드
ALIAS2CANON = {}  # 모든 변형 → 정본명
for c in json.load(open(os.path.join(ROOT, "data", "processed", "cards.json"), encoding="utf-8")):
    CARDS[c["name"]] = c
    ALIAS2CANON[c["name"]] = c["name"]
    for a in c.get("aliases", []):
        ALIAS2CANON[a] = c["name"]
SPOT_PID = {n: CARDS[cn].get("place_id") for n, cn in ALIAS2CANON.items() if CARDS[cn].get("place_id")}
print(f"[server] 카드 정본 {len(CARDS)}장 (이름 변형 {len(ALIAS2CANON)}개, place_id {len(set(SPOT_PID.values()))}곳)")

# ---- 서빙 명단: chroma(cards 컬렉션 = 판정 유지·비폐업만 적재) ----
SERVING = {}  # 정본명 → sources
for _m in col.get(include=["metadatas"])["metadatas"]:
    n = _m.get("spot_name")
    if n:
        e = SERVING.setdefault(n, [])
        if _m.get("source") and _m["source"] not in e:
            e.append(_m["source"])
print(f"[server] 서빙 코퍼스 {len(SERVING)}카페 / {col.count()}문서")

# ---- 이름 매치 레이어 (결정적 조회) ----
# 고유명사 조회는 임베딩의 직업이 아님 — "해지개" top10 전멸 실측 (2026-07-08).
# 사전 = 정본명 + 모든 변형(aliases). 어떤 변형으로 검색해도 정본 카드 1장.
# 폐업 카페도 사전에 남긴다 — 지우면 사용자가 모르고 찾아간다 (신뢰가 컨셉).
_NAME_STOP = {"카페", "커피", "제주", "제주도", "베이커리", "디저트", "브런치",
              "애월", "곽지", "한림", "협재", "함덕", "월정리", "세화", "김녕", "성산",
              "표선", "남원", "위미", "중문", "사계", "대정", "안덕", "우도", "구좌", "조천",
              "제주시내", "서귀포시내", "서귀포", "제주시", "월정"}

def _build_name_index():
    idx = {}  # 정규화 변형 → 정본명
    for alias, canon in ALIAS2CANON.items():
        if canon not in SERVING and not CARDS[canon]["closed"]:
            continue  # 서빙 밖 + 비폐업(보류/제외 판정)은 조회 대상 아님
        key = _norm(alias)
        if len(key) < 2 or key in _NAME_STOP:
            continue
        residual = key
        for sw in _NAME_STOP:
            residual = residual.replace(sw, "")
        if not residual:
            continue  # 스톱워드만으로 조립된 이름 ("애월카페" 아이러니 방지, 실측 2026-07-08)
        idx[key] = canon
    return idx

NAME_IDX = _build_name_index()
print(f"[server] 이름 사전 {len(NAME_IDX)}건 (폐업 안내 포함)")

def name_lookup(q, limit=2):
    """질의에서 카페명 탐지 → 정본명 목록. 긴 이름 우선.
    len>=3은 부분 포함, len==2는 완전 일치만 (오탐 방지)."""
    qn = _norm(q)
    hits = []
    for key, canon in NAME_IDX.items():
        if (len(key) >= 3 and key in qn) or key == qn:
            hits.append((len(key), canon))
    hits.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, canon in hits:
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
        if len(out) >= limit:
            break
    return out

# ---- 질의 지역 해석 (카드 쪽 지역은 merge.py가 주소에서 확정 — 서버는 읽기만) ----
REGIONS = ["애월", "곽지", "한림", "협재", "한경", "함덕", "월정리", "세화", "김녕", "성산",
           "표선", "남원", "위미", "중문", "사계", "대정", "안덕", "우도", "구좌", "조천",
           "제주시내", "서귀포시내"]
ALIAS = {"서귀포": "서귀포시내", "제주시": "제주시내", "월정": "월정리"}
_LABEL2BF = {"협재": ("한림", "협재"), "곽지": ("애월", "곽지"), "월정리": ("구좌", "월정리"),
             "세화": ("구좌", "세화"), "김녕": ("구좌", "김녕"), "종달": ("구좌", "종달"),
             "송당": ("구좌", "송당"), "함덕": ("조천", "함덕"), "위미": ("남원", "위미"),
             "사계": ("안덕", "사계"), "중문": ("서귀포시내", "중문")}

def _label_to_bf(label):
    if not label:
        return None, None
    return _LABEL2BF.get(label, (label, None))

def detect_region(q):
    for r in REGIONS:
        if r in q:
            return r
    for a, std in ALIAS.items():
        if a in q:
            return std
    return None

# ---- 브라우즈 판별: 지역·일반어를 걷어내고 알맹이가 없으면 조건 없는 탐색 ----
_BROWSE_STRIP = sorted(_NAME_STOP | {"추천", "여행", "가볼만한", "가볼만", "곳", "리스트",
                                     "목록", "투어", "베스트", "유명한", "유명"}, key=len, reverse=True)

def is_browse(q):
    r = _norm(q)
    for t in _BROWSE_STRIP:
        r = r.replace(t, "")
    return not r

def _terms(q):
    """질의에서 조건 토큰 추출 — 지역·일반어 제외, 활용형 대비 축소형 포함."""
    out = []
    for tok in re.findall(r"[가-힣a-zA-Z]{2,}", q):
        if _norm(tok) in _NAME_STOP or tok in ("추천", "알려줘", "좋은", "있는", "가볼만한"):
            continue
        stems = {tok}
        if len(tok) >= 3:
            stems.add(tok[:-1])
        if len(tok) >= 4:
            stems.add(tok[:-2])
        out.append(sorted(stems, key=len))
    return out

# ---- 결정적 근거: "왜 이 카페인가"를 코드가 조립 (LLM reason의 후임) ----
# 지어낼 수 없는 이유만: 질의와 겹친 태그 + 지역 일치 + 다수결(고유 블로거 수).
def match_info(q, card, want_b, want_f):
    stems = _terms(q)
    tags_hit = []
    for t in card.get("tags", []):
        tn = _norm(t)
        for st in stems:
            if any(s in tn or (len(_norm(s)) >= 2 and _norm(s) in tn) for s in st):
                tags_hit.append(t)
                break
    info = {}
    if want_b and card.get("region_bucket") == want_b:
        info["region"] = card.get("region_fine") if (want_f and card.get("region_fine") == want_f) \
            else card.get("region_bucket")
    if tags_hit:
        info["tags"] = tags_hit[:4]
    if card.get("bloggers"):
        info["bloggers"] = card["bloggers"]
    return info

def card_out(canon, score, sources, name_match=False, q="", want_b=None, want_f=None):
    """카드 정본 → /search 응답 카드 (하위호환: summary_blog 자리에 정본 summary)."""
    c = CARDS.get(canon) or {"name": canon, "closed": False}
    g = c.get
    out = {"spot_name": c["name"], "place_id": g("place_id"),
           "score": round(score, 3), "sources": sources,
           "region": g("region_fine") or g("region_bucket"),
           "region_bucket": g("region_bucket"), "region_fine": g("region_fine"),
           "summary_blog": g("summary", ""), "summary_youtube": "",
           "tags": g("tags", []), "video_ids": g("video_ids", [])[:3],
           "blog_links": g("blog_links", []), "address": g("address", ""),
           "category": g("category", ""),
           "mention_count": g("mention_count", 0), "bloggers": g("bloggers", 0),
           "caution": g("caution", []), "hours_hint": g("hours_hint", ""),
           "reaction_tone": g("reaction_tone", ""), "reaction_hint": g("reaction_hint", ""),
           "rating_avg": g("rating_avg"), "rating_count": g("rating_count"),
           "review_tone": g("review_tone", ""),
           "closed": g("closed", False),
           "lat": g("lat"), "lng": g("lng")}
    if name_match:
        out["name_match"] = True
    m = match_info(q, c, want_b, want_f)
    if m:
        out["matched"] = m
    return out


def _places_photo_uris(name, lat, lng, place_id=None, limit=8):
    """Places(New)로 카페 사진의 임시 URL 목록. photo name은 저장 금지·만료 대상이라 매 호출 신선하게."""
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


# ---- /evidence: 질의 키워드 → 원문 근거 (전부 코드 — LLM 은퇴 2026-07-09) ----
# "블로거 21명이 언급" + 원문 발췌 그대로. 스니펫은 정본매핑으로 접어 조각 전체에서 모은다.
_SNIPPETS = None          # 정본명 → [{t, date, blogger, link}]
_REACTIONS = None         # video_id → {summary, tone, n_comments}
_REVIEWS = None           # 정본명 → [{text, star}]  (카카오 방문자 리뷰)

def _load_snippets():
    global _SNIPPETS, _REACTIONS, _REVIEWS
    if _SNIPPETS is not None:
        return
    snip = {}
    for raw_name in ("네이버 크롤링.jsonl", "네이버 재검색 크롤링.jsonl"):
        p = os.path.join(ROOT, "data", "raw", raw_name)
        if not os.path.exists(p):
            continue
        for line in open(p, encoding="utf-8", errors="replace"):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            key = _norm(rec.get("cleaned_name") or rec["spot_name"])
            canon = ALIAS2CANON.get(rec["spot_name"], rec["spot_name"])
            rows = []
            for it in rec.get("blog", {}).get("items", []):
                txt = _clean(it.get("title", "") + " " + it.get("description", ""))
                if key and key in _norm(txt) and it.get("postdate", "") >= "20240101":
                    rows.append({"t": txt, "date": it.get("postdate", ""),
                                 "blogger": it.get("bloggername", ""), "link": it.get("link", "")})
            if rows:
                snip.setdefault(canon, []).extend(rows)
    _SNIPPETS = snip
    rx = {}
    p = os.path.join(ROOT, "data", "processed", "댓글 정제.jsonl")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("reaction_summary"):
                rx[r["video_id"]] = {"summary": r["reaction_summary"],
                                     "tone": r.get("reaction_tone", ""),
                                     "n_comments": r.get("n_comments", 0)}
    _REACTIONS = rx
    rv = {}
    p = os.path.join(ROOT, "data", "processed", "카카오리뷰.jsonl")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("spot_name") and r.get("reviews"):
                canon = ALIAS2CANON.get(r["spot_name"], r["spot_name"])
                rv.setdefault(canon, []).extend(r["reviews"])
    _REVIEWS = rv
    print(f"[server] 근거 인덱스: 스니펫 {sum(len(v) for v in snip.values())}건/{len(snip)}카페, "
          f"반응 {len(rx)}편, 카카오리뷰 {sum(len(v) for v in rv.values())}건/{len(rv)}카페")

def _pick_review_quotes(reviews, terms, limit=3):
    """카카오 리뷰 대표 인용 — 코드 선별. 질의 키워드 우선, 낮은 별점 하나 반드시 포함 (신뢰가 컨셉)."""
    def hit(t):
        return not terms or any(any(s in t for s in stems) for stems in terms)
    cands = [r for r in reviews if (r.get("text") or "").strip() and hit(r["text"])]
    if not cands:
        return []
    low = sorted([r for r in cands if (r.get("star") or 5) <= 3], key=lambda r: r.get("star") or 5)
    high = [r for r in cands if (r.get("star") or 5) >= 4]
    out, seen = [], set()
    for r in ((high[:limit - 1] + low[:1]) if low else high[:limit]):
        t = (r["text"] or "").strip().replace("\n", " ")
        if t not in seen:
            seen.add(t)
            out.append({"text": t[:140], "star": r.get("star")})
    return out[:limit]

def _pick_blog_quotes(rows, terms, limit=3):
    """블로그 스니펫 원문 발췌 — 키워드 매칭 문장, 블로거 다양성 우선. 발췌는 원문 그대로(지어내기 원천 차단)."""
    out, seen_bloggers = [], set()
    for r in rows:  # 1순위: 서로 다른 블로거의 문장
        if r["blogger"] in seen_bloggers:
            continue
        seen_bloggers.add(r["blogger"])
        out.append(r["t"][:160])
        if len(out) >= limit:
            return out
    for r in rows:  # 블로거가 모자라면 중복 허용
        t = r["t"][:160]
        if t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out

def _evidence_impl(name: str, q: str = ""):
    _load_snippets()
    canon = ALIAS2CANON.get(name, name)
    rows = _SNIPPETS.get(canon, [])
    terms = _terms(q) if q else []
    if terms:
        matched = [r for r in rows if any(any(s in r["t"] for s in stems) for stems in terms)]
    else:
        matched = rows
    bloggers = len({r["blogger"] for r in matched if r["blogger"]})
    c = CARDS.get(canon, {})
    out = {"name": canon, "query": q,
           "n_snippets": len(rows), "n_matched": len(matched), "bloggers_matched": bloggers,
           "quotes": _pick_blog_quotes(matched, terms), "opinion": "",
           "rating_avg": c.get("rating_avg"), "rating_count": c.get("rating_count"),
           "review_tone": c.get("review_tone", ""),
           "review_quotes": _pick_review_quotes((_REVIEWS or {}).get(canon, []), terms),
           "youtube_reactions": []}
    for vid in (c.get("video_ids") or [])[:3]:
        r = (_REACTIONS or {}).get(vid)
        if r:
            out["youtube_reactions"].append({"video_id": vid, **r})
    return out


app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/evidence")
def evidence(name: str, q: str = ""):
    """카드 '근거 보기' — 실제 블로거 원문 인용 + 방문자 리뷰 + 쇼츠 댓글 반응 (전부 코드 발췌)."""
    return _evidence_impl(name, q)

@app.get("/search")
def search(q: str, k: int = 8, explain: int = 0, debug: int = 0):
    # explain 파라미터는 하위호환으로 받기만 함 (LLM 층 은퇴 — 항상 결정적 응답)
    region = detect_region(q)
    want_b, want_f = _label_to_bf(region)
    pinned = name_lookup(q)  # 이름 조회는 임베딩보다 먼저, 결정적으로
    browse = not pinned and is_browse(q)
    relaxed = False
    total = None
    emb_debug = None

    if browse:
        # 브라우즈: 빈 질의는 유사도가 노이즈 — 임베딩 생략, 다수결(union 블로거) 정렬 (원칙 8)
        k = max(k, 12)
        pool = []
        for n in SERVING:
            c = CARDS.get(n, {})
            b, f = c.get("region_bucket"), c.get("region_fine")
            if want_b:
                if want_f and f == want_f:
                    tier = 0
                elif b == want_b:
                    tier = 1 if want_f else 0
                else:
                    continue
            else:
                tier = 0
            pool.append((tier, -c.get("bloggers", 0), -c.get("mention_count", 0), n))
        total = len(pool)
        pool.sort()
        ordered = [(n, 0.0, SERVING[n], False) for _, _, _, n in pool[:k]]
    else:
        q_emb = client.embeddings.create(model="text-embedding-3-large", input=[q]).data[0].embedding
        res = col.query(query_embeddings=[q_emb], n_results=max(k * 8, 64))

        if debug:
            emb_debug = sorted(
                [{"spot_name": m["spot_name"], "source": m.get("source"), "score": round(1 - d, 3)}
                 for m, d in zip(res["metadatas"][0], res["distances"][0])],
                key=lambda x: -x["score"])

        # spot 단위 병합: 같은 카페(blog+hybrid 문서)의 최고 점수 — 이름 중복은 merge가 이미 해소
        spots = {}
        for meta, dist in zip(res["metadatas"][0], res["distances"][0]):
            n = meta["spot_name"]
            score = 1 - dist
            s = spots.setdefault(n, {"score": 0.0, "sources": []})
            s["score"] = max(s["score"], score)
            if meta["source"] not in s["sources"]:
                s["sources"].append(meta["source"])

        # 이름 매치 고정: 질의에 카페명이 있으면 지역 필터와 무관하게 무조건 앞 (폐업이면 안내 카드)
        ordered = []
        for canon in pinned:
            s = spots.pop(canon, None) or {"sources": SERVING.get(canon, [])}
            ordered.append((canon, 1.0, s["sources"], True))  # 확정 매치는 유사도가 아니라 1.0 고정

        # 지역 필터 (카드 확정값 기준, 단계 완화: 세부 → 버킷 → 전체)
        def bf(n):
            c = CARDS.get(n, {})
            return c.get("region_bucket"), c.get("region_fine")
        pool = [(n, s["score"], s["sources"]) for n, s in spots.items()]
        _sc = lambda x: -x[1]
        if want_b:
            tier1 = [p for p in pool if want_f and bf(p[0])[1] == want_f]
            tier2 = [p for p in pool if bf(p[0])[0] == want_b and p not in tier1]
            regional = sorted(tier1, key=_sc) + sorted(tier2, key=_sc)
            if len(ordered) + len(regional) >= k:
                pool = regional
            else:
                relaxed = True
                others = sorted([p for p in pool if p not in regional], key=_sc)
                pool = regional + others
        else:
            pool = sorted(pool, key=_sc)
        ordered = (ordered + [(n, sc, src, False) for n, sc, src in pool])[:k]

    cards = [card_out(n, sc, src, nm, q=q, want_b=want_b, want_f=want_f)
             for n, sc, src, nm in ordered]

    out = {"query": q, "region": region, "relaxed": relaxed, "browse": browse,
           "total": total, "cards": cards}

    # 디버깅 모드: 후보 소집~정렬을 유리상자로 (?debug=1) — LLM 관문은 은퇴, 감시 지점 소멸
    if debug:
        out["debug"] = {
            "route": "조회" if pinned else ("브라우즈" if browse else "조건"),
            "region": {"detected": region, "bucket": want_b, "fine": want_f, "relaxed": relaxed},
            "pinned": pinned,
            "embedding": ({"n_docs": len(emb_debug),
                           "n_unique_cafes": len({d["spot_name"] for d in emb_debug}),
                           "docs": emb_debug[:40]} if emb_debug is not None else None),
            "final_order": [c["spot_name"] for c in cards],
            "matched": {c["spot_name"]: c.get("matched") for c in cards if c.get("matched")},
        }
    return out

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
    return {"ok": True, "docs": col.count(), "cafes": len(CARDS), "serving": len(SERVING),
            "place_id_map_size": len(SPOT_PID),
            "place_id_test": {n: SPOT_PID.get(n) for n in ("노을리", "노을리카페", "프릳츠커피")}}
