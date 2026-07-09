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
            "lat": None, "lng": None,
            "caution": [], "hours_hint": ""}

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
    # 동료 시드 라벨 (2026-07-09 편입): 태그·주의 신호·영업시간 힌트 — 표시층 전용, 임베딩 금지
    lp = os.path.join(ROOT, "data", "processed", "시드라벨.json")
    if os.path.exists(lp):
        for n, lab in json.load(open(lp, encoding="utf-8")).items():
            a = aux.setdefault(n, _blank())
            a["tags"].update(lab.get("tags_seed") or [])
            a["caution"] = lab.get("caution") or []
            a["hours_hint"] = lab.get("hours_hint") or ""
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

_ALL_META = col.get(include=["metadatas"])["metadatas"]
SERVING = {}  # 서빙 코퍼스의 카페 전체: spot_name → {label, sources} (브라우즈 모드 명단)
for _m in _ALL_META:
    if _m.get("spot_name"):
        _e = SERVING.setdefault(_m["spot_name"], {"label": _m.get("region"), "sources": []})
        if _m.get("source") and _m["source"] not in _e["sources"]:
            _e["sources"].append(_m["source"])

def _build_name_index():
    idx = {}  # 정규화 이름 → {name, region, sources}
    meta = _ALL_META
    for m in meta:
        n = m.get("spot_name")
        if not n:
            continue
        key = _norm(n)
        if len(key) < 2 or key in _NAME_STOP:
            continue
        # 스톱워드만으로 조립된 이름 제외 — "애월카페"가 "애월 카페" 질의에 걸리는 아이러니 방지 (실측 2026-07-08)
        residual = key
        for sw in _NAME_STOP:
            residual = residual.replace(sw, "")
        if not residual:
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

REGIONS = ["애월", "곽지", "한림", "협재", "한경", "함덕", "월정리", "세화", "김녕", "성산",
           "표선", "남원", "위미", "중문", "사계", "대정", "안덕", "우도", "구좌", "조천",
           "제주시내", "서귀포시내"]
ALIAS = {"서귀포": "서귀포시내", "제주시": "제주시내", "월정": "월정리"}

# ---- 지역 교정: 주소가 정답, LLM 추측 라벨은 폴백 ----
# 배경: chroma region 라벨(Pass 1 LLM 추측)을 주소와 대조한 결과 불일치 24.6% 실측 (2026-07-08).
#       지역도 식별자다(원칙 5 연장) — 주소에서 코드로 유도. 체계는 2단: 버킷(읍면급) / 세부(리·동급).
_EMD2BUCKET = {"애월읍": "애월", "한림읍": "한림", "한경면": "한경", "구좌읍": "구좌",
               "조천읍": "조천", "성산읍": "성산", "표선면": "표선", "남원읍": "남원",
               "안덕면": "안덕", "대정읍": "대정", "우도면": "우도", "추자면": "추자"}
_FINE_TOKENS = {"협재리": ("한림", "협재"), "곽지리": ("애월", "곽지"),
                "월정리": ("구좌", "월정리"), "세화리": ("구좌", "세화"),
                "김녕리": ("구좌", "김녕"), "종달리": ("구좌", "종달"), "송당리": ("구좌", "송당"),
                "함덕리": ("조천", "함덕"), "위미리": ("남원", "위미"),
                "사계리": ("안덕", "사계"), "중문동": ("서귀포시내", "중문"),
                "색달동": ("서귀포시내", "중문")}
# 리급 라벨 → (버킷, 세부): 질의어·구 라벨을 같은 계층으로 해석
_LABEL2BF = {"협재": ("한림", "협재"), "곽지": ("애월", "곽지"), "월정리": ("구좌", "월정리"),
             "세화": ("구좌", "세화"), "김녕": ("구좌", "김녕"), "종달": ("구좌", "종달"),
             "종달리": ("구좌", "종달"), "송당": ("구좌", "송당"), "함덕": ("조천", "함덕"),
             "위미": ("남원", "위미"), "사계": ("안덕", "사계"), "중문": ("서귀포시내", "중문")}

def addr_to_region(a):
    """주소 → (버킷, 세부). 주소 없거나 판별 불가면 (None, None)."""
    if not a:
        return None, None
    for tok, bf in _FINE_TOKENS.items():
        if tok in a:
            return bf
    m = re.search(r"(제주시|서귀포시)\s*(\S+[읍면])?", a)
    if not m:
        return None, None
    if m.group(2) in _EMD2BUCKET:
        return _EMD2BUCKET[m.group(2)], None
    return ("제주시내", None) if m.group(1) == "제주시" else ("서귀포시내", None)

def _label_to_bf(label):
    if not label or label in ("기타", "NONE"):
        return None, None
    return _LABEL2BF.get(label, (label, None))

def _load_spot_loc():
    import csv
    loc = {}
    path = os.path.join(ROOT, "data", "processed", "review_master.csv")
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            b, f = addr_to_region(r.get("지역검색_주소", ""))
            if b:
                loc[r["카페명"]] = (b, f)
    return loc

SPOT_LOC = _load_spot_loc()
print(f"[server] 지역 교정(주소 기반) {len(SPOT_LOC)}카페")

def spot_bf(name, label=None):
    """카페의 (버킷, 세부) — 주소 유도값 우선, 없으면 구 라벨 폴백."""
    return SPOT_LOC.get(name) or _label_to_bf(label)

def detect_region(q):
    for r in REGIONS:
        if r in q:
            return r
    for a, std in ALIAS.items():
        if a in q:
            return std
    return None

# ---- 브라우즈 판별: 지역·일반어를 걷어내고 알맹이가 없으면 조건 없는 탐색 ----
# "애월 카페" 같은 빈 질의는 유사도 정렬이 노이즈 — 동점 정렬 원칙(8)대로 다수결(고유 블로거 수)로.
_BROWSE_STRIP = sorted(_NAME_STOP | {"추천", "여행", "가볼만한", "가볼만", "곳", "리스트",
                                     "목록", "투어", "베스트", "유명한", "유명"}, key=len, reverse=True)

def is_browse(q):
    r = _norm(q)
    for t in _BROWSE_STRIP:
        r = r.replace(t, "")
    return not r


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


# ---- [2.5+3] LLM 선별 + 근거 설명 ----
# 임베딩=후보 소집(재현율), LLM=판단(정밀도). id는 코드가 운반(원칙 5), 실패해도 검색은 무사.
# 입력에 인기 수치(블로거·언급수) 미포함 — LLM도 큰 숫자에 홀린다 (원칙 8의 LLM 버전).
_LLM_SYS = (
    "너는 제주 카페 검색 도우미다. 사용자의 질문과 카페 후보 목록(JSON)을 보고 JSON으로만 답하라.\n"
    '형식: {"intro": "질문에 대한 1~2문장 응답", "picks": [질문에 맞는 순서대로 i 배열], '
    '"reasons": {"0": "그 카페를 추천하는 이유 한 줄", ...}}\n'
    "규칙:\n"
    "- 각 카페의 summary/tags에 적힌 내용만 근거로 쓸 것. 없는 사실을 지어내지 말 것.\n"
    "- 질문과 무관한 카페는 picks에서 빼도 된다. 단 name_match=true 카페는 항상 포함.\n"
    "- reasons는 근거가 있는 카페만. 이유는 담백하게, 과장 광고체 금지.")

def _llm_annotate(q, cards):
    """카드 선별·정렬·이유 생성. 어떤 실패에도 (원래 순서, intro 없음)으로 폴백."""
    items = [{"i": i, "name": c["spot_name"], "region": c["region"],
              "name_match": bool(c.get("name_match")),
              "tags": (c.get("tags") or [])[:8],
              "summary": (c.get("summary_blog") or c.get("summary_youtube") or "")[:220]}
             for i, c in enumerate(cards)]
    resp = client.chat.completions.create(
        model="gpt-5-mini",
        response_format={"type": "json_object"},
        max_completion_tokens=4000,
        reasoning_effort="minimal",
        timeout=20,
        messages=[{"role": "system", "content": _LLM_SYS},
                  {"role": "user", "content": json.dumps({"질문": q, "카페들": items}, ensure_ascii=False)}],
    )
    d = json.loads(resp.choices[0].message.content or "{}")
    reasons = {int(i): str(v) for i, v in (d.get("reasons") or {}).items() if str(i).isdigit()}
    picks = [i for i in (d.get("picks") or []) if isinstance(i, int) and 0 <= i < len(cards)]
    # 재배열은 코드가 집행: 이름 매치는 무조건 앞, picks 순서, 누락분은 뒤에 원래 순서로
    for i, c in enumerate(cards):
        if i in reasons:
            c["reason"] = reasons[i]
    front = [i for i, c in enumerate(cards) if c.get("name_match")]
    rest = [i for i in picks if i not in front] + [i for i in range(len(cards))
                                                  if i not in picks and i not in front]
    return str(d.get("intro") or ""), [cards[i] for i in front + rest]


# ---- /evidence: 질의 키워드 → 원문 근거 (블로그 스니펫 인용 + 쇼츠 댓글 반응) ----
# 배경(민옥 2026-07-09): "블로거 40명이 언급"이 아니라 "그 사람들이 실제로 뭐라고 했는지"가 근거.
# 원칙: 인용은 원문 발췌만 (지어내기 금지). 스니펫 인덱스는 첫 호출 때 lazy 로드.
_SNIPPETS = None          # spot_name → [{t, d, date, blogger}]
_REACTIONS = None         # video_id → {summary, tone, n}

def _load_snippets():
    global _SNIPPETS, _REACTIONS
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
            rows = []
            for it in rec.get("blog", {}).get("items", []):
                txt = _clean(it.get("title", "") + " " + it.get("description", ""))
                if key and key in _norm(txt) and it.get("postdate", "") >= "20240101":
                    rows.append({"t": txt, "date": it.get("postdate", ""),
                                 "blogger": it.get("bloggername", ""), "link": it.get("link", "")})
            if rows:
                snip.setdefault(rec["spot_name"], []).extend(rows)
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
    print(f"[server] 근거 인덱스: 스니펫 {sum(len(v) for v in snip.values())}건/{len(snip)}카페, 반응 {len(rx)}편")

def _terms(q):
    """질의에서 의견 검색용 알맹이 토큰 추출 — 지역·일반어 제외, 활용형 대비 축소형 포함."""
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

_EV_SYS = ("너는 검색 근거 요약가다. 특정 카페에 대한 실제 블로그 문장들과 사용자의 관심 키워드를 받는다. "
           "JSON으로만 답하라: {\"opinion\": \"사람들이 그 키워드에 대해 실제로 말하는 바 1~2문장\", "
           "\"quotes\": [\"원문에서 그대로 발췌한 짧은 인용 (최대 3개)\"]}. "
           "규칙: quotes는 반드시 입력 문장의 부분 문자열일 것. 문장에 없는 내용 금지. 광고체 금지.")

def _evidence_impl(name: str, q: str = ""):
    _load_snippets()
    rows = _SNIPPETS.get(name, [])
    terms = _terms(q) if q else []
    if terms:
        matched = [r for r in rows if any(any(s in r["t"] for s in stems) for stems in terms)]
    else:
        matched = rows
    bloggers = len({r["blogger"] for r in matched if r["blogger"]})
    top = matched[:12]
    out = {"name": name, "query": q,
           "n_snippets": len(rows), "n_matched": len(matched), "bloggers_matched": bloggers,
           "quotes": [], "opinion": "",
           "youtube_reactions": []}
    a = AUX.get(name, {})
    for vid in (a.get("video_ids") or [])[:3]:
        r = (_REACTIONS or {}).get(vid)
        if r:
            out["youtube_reactions"].append({"video_id": vid, **r})
    if top:
        try:
            resp = client.chat.completions.create(
                model="gpt-5-mini", response_format={"type": "json_object"},
                max_completion_tokens=4000, reasoning_effort="minimal", timeout=20,
                messages=[{"role": "system", "content": _EV_SYS},
                          {"role": "user", "content": json.dumps(
                              {"카페": name, "키워드": q, "문장들": [r["t"][:200] for r in top]},
                              ensure_ascii=False)}])
            d = json.loads(resp.choices[0].message.content or "{}")
            src = " ".join(r["t"] for r in top)
            out["opinion"] = str(d.get("opinion") or "")
            out["quotes"] = [str(x) for x in (d.get("quotes") or []) if str(x) and str(x) in src][:3]
        except Exception as e:
            print(f"[server] evidence LLM 실패: {type(e).__name__}: {e}")
    return out


app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/evidence")
def evidence(name: str, q: str = ""):
    """카드 '근거 보기' — 질의 키워드에 대한 실제 블로거 의견(원문 인용)과 쇼츠 댓글 반응."""
    return _evidence_impl(name, q)

@app.get("/search")
def search(q: str, k: int = 8, explain: int = 1):
    region = detect_region(q)
    want_b, want_f = _label_to_bf(region)
    pinned = name_lookup(q)  # 이름 조회는 임베딩보다 먼저, 결정적으로
    browse = not pinned and is_browse(q)
    relaxed = False
    total = None

    if browse:
        # 브라우즈: 빈 질의는 유사도가 노이즈 — 임베딩 생략, 다수결(고유 블로거 수) 정렬 (원칙 8의 동점 정렬)
        k = max(k, 12)
        pool = []
        for n, e in SERVING.items():
            b, f = spot_bf(n, e["label"]) or (None, None)
            if want_b:
                if want_f and f == want_f:
                    tier = 0
                elif b == want_b:
                    tier = 1 if want_f else 0
                else:
                    continue
            else:
                tier = 0
            a = AUX.get(n, {})
            pool.append({"spot_name": n, "score": 0.0, "sources": e["sources"], "bf": (b, f),
                         "_key": (tier, -a.get("bloggers", 0), -a.get("mention_count", 0))})
        total = len(pool)
        pool.sort(key=lambda s: s.pop("_key"))
        ordered = pool[:k]
    else:
        q_emb = client.embeddings.create(model="text-embedding-3-large", input=[q]).data[0].embedding
        # 지역 필터는 chroma where(오염 라벨) 대신 교정값으로 코드에서 — 후보를 넉넉히 소집
        res = col.query(query_embeddings=[q_emb], n_results=max(k * 8, 64))

        # spot 단위 병합: 같은 카페의 문서 중 최고 점수 + 교정 지역 부여
        spots = {}
        for meta, dist in zip(res["metadatas"][0], res["distances"][0]):
            n = meta["spot_name"]
            score = 1 - dist
            s = spots.setdefault(n, {"spot_name": n, "score": 0.0, "sources": [],
                                     "bf": spot_bf(n, meta.get("region"))})
            s["score"] = max(s["score"], score)
            if meta["source"] not in s["sources"]:
                s["sources"].append(meta["source"])

        # 이름 매치 고정: 질의에 카페명이 있으면 지역 필터와 무관하게 무조건 앞
        ordered = []
        for e in pinned:
            p = spots.pop(e["name"], None) or {"spot_name": e["name"], "score": 1.0,
                                               "sources": e["sources"],
                                               "bf": spot_bf(e["name"], e["region"])}
            p["name_match"] = True
            ordered.append(p)

        # 지역 필터 (교정값 기준, 단계 완화: 세부 → 버킷 → 전체)
        pool = list(spots.values())
        _sc = lambda s: -s["score"]
        if want_b:
            tier1 = [s for s in pool if want_f and s["bf"][1] == want_f]
            tier2 = [s for s in pool if s["bf"][0] == want_b and s not in tier1]
            regional = sorted(tier1, key=_sc) + sorted(tier2, key=_sc)
            if len(ordered) + len(regional) >= k:
                pool = regional
            else:
                relaxed = True
                others = sorted([s for s in pool if s not in regional], key=_sc)
                pool = regional + others
        else:
            pool = sorted(pool, key=_sc)
        ordered = (ordered + pool)[:k]

    cards = []
    for s in ordered:
        n = s["spot_name"]
        a = AUX.get(n, {})
        b, f = s.pop("bf", (None, None))
        cards.append({**s,
                      "score": round(s["score"], 3),
                      "region": f or b,            # 표시용 (세부 우선)
                      "region_bucket": b,          # 읍면급
                      "region_fine": f,            # 리·동급 (없으면 null)
                      "summary_youtube": a.get("summary_youtube", ""),
                      "summary_blog": a.get("summary_blog", ""),
                      "tags": sorted(a.get("tags", [])),
                      "video_ids": a.get("video_ids", [])[:3],
                      "blog_links": a.get("blog_links", []),
                      "mention_count": a.get("mention_count", 0),
                      "bloggers": a.get("bloggers", 0),
                      "caution": a.get("caution", []),
                      "hours_hint": a.get("hours_hint", ""),
                      "lat": a.get("lat"), "lng": a.get("lng")})

    # [2.5+3] LLM 선별+이유 — 브라우즈는 지분 순서 유지(주석만), 조건 질의는 재정렬 허용
    intro = ""
    if explain and cards:
        try:
            intro, annotated = _llm_annotate(q, cards)
            if not browse:
                cards = annotated
        except Exception as e:
            print(f"[server] LLM 주석 실패 (검색은 무사): {type(e).__name__}: {e}")

    return {"query": q, "region": region, "relaxed": relaxed, "browse": browse,
            "total": total, "intro": intro, "cards": cards}

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
