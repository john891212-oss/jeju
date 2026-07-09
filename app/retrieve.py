# -*- coding: utf-8 -*-
"""
W3 -- 태그 기반 후보 선정 + 결정론 정렬 (LLM 호출 0회)

역할:
  retrieve(trace, k=8) -> trace
  router(W2)가 채운 intent(유형/지역/하드/소프트/배제)를 받아
  cards.json 정본에서 후보를 골라 결정론적으로 정렬하고,
  trace의 funnel/results만 채워 돌려준다 (TraceState 순수 함수 -- W5 배선 대비).

설계 원칙:
  - LLM 없음. 검색의 몸통은 전부 코드 -- "LLM에게 검색 도구를 줬다" 구도의 도구 쪽.
  - 서빙 필터 = closed==False 이고 판정=="유지" (chroma 없이 server.py SERVING 근사).
  - 조회(이름)는 폐업 카드도 안내용으로 응답 (server.py:22 원칙 -- 지우면 모르고 찾아간다).
  - 소프트 조건이 있으면 태그충족 0인 카드는 후보에서 제외한다 --
    0건이어야 relax 사다리(app/relax.py)가 발동한다. 전부 0점인데 후보로 남으면
    사다리가 안 작동하고 "조건과 무관한 카드"가 조건 검색 결과로 둔갑한다.
  - 정렬은 완전 결정론: 동률의 끝은 항상 이름 사전순. 같은 입력 = 같은 출력.
  - 지역 tier(세부 일치 > 버킷 일치 > 인접 확장)가 태그충족보다 우선한다 --
    server.py:461~463(세부 tier1 먼저, 버킷 tier2 나중)의 정렬 원칙 미러.
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # server.py:38 ROOT 패턴 미러

# ---- 정규화 (server.py:62~67 미러 -- 정본은 server.py) ----
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s):
    return _TAG_RE.sub("", s or "").strip()


def _norm(s):
    s = _clean(s).split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())


# ---- 지역 라벨 -> (버킷, 세부) 2단 계층 (server.py:127 미러 -- 정본은 server.py) ----
# 자체 지리 매핑 발명 금지 -- 카드 쪽 지역은 merge.py가 주소에서 확정, 여기는 읽기만.
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


# ---- 카드 정본 로드 + 서빙 필터 ----
_CARDS_PATH = os.path.join(ROOT, "data", "processed", "cards.json")
with open(_CARDS_PATH, encoding="utf-8") as _f:
    ALL_CARDS = json.load(_f)

CARDS = {}        # 정본명 -> 카드 (server.py:70~76 미러)
ALIAS2CANON = {}  # 모든 변형 -> 정본명
for _c in ALL_CARDS:
    CARDS[_c["name"]] = _c
    ALIAS2CANON[_c["name"]] = _c["name"]
    for _a in _c.get("aliases", []):
        ALIAS2CANON[_a] = _c["name"]

# 서빙 = 비폐업 & 판정 유지 (chroma cards 컬렉션 적재 기준의 근사 -- server.py:80~88 참조)
SERVING_CARDS = [c for c in ALL_CARDS if not c.get("closed") and c.get("판정") == "유지"]
print(f"[retrieve] 카드 정본 {len(ALL_CARDS)}장, 서빙 {len(SERVING_CARDS)}장 "
      f"(closed=False & 판정=유지), 이름 변형 {len(ALIAS2CANON)}개")

# ---- 이름 매치 레이어 (server.py:94~134 미러 -- 조회 폴백용, 정본은 server.py) ----
# 고유명사 조회는 임베딩의 직업이 아님 ("해지개" top10 전멸 실측 2026-07-08).
_NAME_STOP = {"카페", "커피", "제주", "제주도", "베이커리", "디저트", "브런치",
              "애월", "곽지", "한림", "협재", "함덕", "월정리", "세화", "김녕", "성산",
              "표선", "남원", "위미", "중문", "사계", "대정", "안덕", "우도", "구좌", "조천",
              "제주시내", "서귀포시내", "서귀포", "제주시", "월정"}


def _servable_for_lookup(canon):
    """조회 응답 가능 = 서빙(유지&비폐업) 또는 폐업(안내용). 비폐업인데 보류/제외 판정은 제외.
    server.py:102~103(서빙 밖 + 비폐업은 조회 대상 아님) 원칙의 chroma 없는 근사."""
    c = CARDS[canon]
    return bool(c.get("closed")) or c.get("판정") == "유지"


def _build_name_index():
    idx = {}  # 정규화 변형 -> 정본명
    for alias, canon in ALIAS2CANON.items():
        if not _servable_for_lookup(canon):
            continue
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


def name_lookup(q, limit=2):
    """질의에서 카페명 탐지 -> 정본명 목록. 긴 이름 우선.
    len>=3은 부분 포함, len==2는 완전 일치만 (오탐 방지). server.py:118~134 미러."""
    qn = _norm(q)
    hits = []
    for key, canon in NAME_IDX.items():
        if (len(key) >= 3 and key in qn) or key == qn:
            hits.append((len(key), canon))
    hits.sort(key=lambda x: (-x[0], x[1]))  # 길이 동률은 이름순 -- 결정론 보강
    seen, out = set(), []
    for _, canon in hits:
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
        if len(out) >= limit:
            break
    return out


# ---- 반응 점수: reaction_tone -> {-1, 0, 1} (정렬 보조축) ----
_TONE_SCORE = {"긍정": 1, "혼합": 0, "부정": -1}  # 중립/빈값은 0


def _reaction(c):
    return _TONE_SCORE.get((c.get("reaction_tone") or "").strip(), 0)


def _result_entry(c, tag_hits):
    """TraceState.results[] 원소. 다수결 = 고유 블로거 수 (인기 원값은 정렬에만 -- 확정 결정 25)."""
    return {"spot_name": c["name"],
            "score_parts": {"태그충족": tag_hits,
                            "다수결": c.get("bloggers", 0),
                            "반응": _reaction(c)}}


def _retrieve_lookup(trace, intent):
    """조회(특정 카페명): 해당 카드 그대로. 폐업이어도 안내용으로 포함.

    이름 필드는 "이름" 우선, 없으면 "pinned"(router가 쓰는 확장 필드) --
    W2-4 조립 검증에서 발견된 trace 필드 불일치 보정 (2026-07-09)."""
    names = intent.get("이름") or intent.get("pinned") or []
    if isinstance(names, str):
        names = [names]
    canons = []
    for nm in names:
        canon = ALIAS2CANON.get(nm) or NAME_IDX.get(_norm(nm))
        if canon and canon not in canons:
            canons.append(canon)
        elif not canon:
            print(f"[retrieve] 경고: 조회 이름 '{nm}' 사전에 없음")
    if not names:  # router가 이름을 안 채웠으면 질의에서 직접 탐지 (폴백)
        canons = name_lookup(trace.get("query") or "")
    out = []
    for canon in canons:
        if not _servable_for_lookup(canon):
            print(f"[retrieve] 안내: '{canon}' 판정={CARDS[canon].get('판정')} -- 조회 응답 제외")
            continue
        c = CARDS[canon]
        e = _result_entry(c, 0)
        e["closed"] = bool(c.get("closed"))  # 폐업 안내 플래그 -- W4 종합이 "폐업했어요" 문장에 사용
        e["판정"] = c.get("판정")
        out.append(e)
    trace["funnel"].append({"stage": "조회:" + ("+".join(names) if names else "질의탐지"),
                            "n": len(out)})
    trace["results"] = out
    return trace


def retrieve(trace, k=8):
    """태그 기반 후보 선정 + 결정론 정렬. trace의 funnel/results만 채워 반환.

    - 조회: intent.이름(없으면 질의 탐지)의 카드 그대로 (폐업 포함).
    - 조건검색: 지역 -> 배제 -> 하드 -> 소프트 순 필터, 각 단계 funnel 기록.
      정렬 (tier, -태그충족, -다수결, -반응, 이름) -- 완전 결정론.
    - 브라우즈(소프트 없음): 지역 필터만, (tier, -다수결, -언급수, 이름) 정렬
      (server.py:408~427 다수결 원칙 미러, k는 최소 12로 -- server.py:410 미러).
    - trace.region_expanded(dict, relax가 세팅)가 있으면 인접 버킷도 tier 2로 포함.
    """
    intent = trace.get("intent") or {}
    trace.setdefault("funnel", [])
    trace["results"] = []  # relax 재호출 대비 -- results는 항상 이번 검색 것만
    kind = intent.get("유형") or "조건검색"

    if kind == "조회":
        return _retrieve_lookup(trace, intent)

    pool = list(SERVING_CARDS)
    trace["funnel"].append({"stage": "전체", "n": len(pool)})

    # -- 지역 필터 + tier (server.py:414~423, 444~453 tier 논리 미러) --
    tier_of = {}  # 정본명 -> tier (0=세부 일치 또는 버킷 직접, 1=세부 지정 시 버킷, 2=인접 확장)
    region_raw = intent.get("지역")
    if region_raw:
        region = ALIAS.get(region_raw, region_raw)
        if region not in REGIONS and region not in _LABEL2BF:
            print(f"[retrieve] 경고: 미인식 지역 라벨 '{region_raw}' -- 버킷 직접 대조로 진행")
        want_b, want_f = _label_to_bf(region)
        exp = trace.get("region_expanded")
        exp_to = list(exp.get("to") or []) if isinstance(exp, dict) else []
        kept = []
        for c in pool:
            b, f = c.get("region_bucket"), c.get("region_fine")
            if b is None:
                continue  # 지역미상 카드(버킷 None)는 지역 필터 활성 시 제외
            if want_f and f == want_f:
                t = 0
            elif b == want_b:
                t = 1 if want_f else 0
            elif b in exp_to:
                t = 2  # 인접 확장분은 원 지역 카드보다 항상 뒤
            else:
                continue
            tier_of[c["name"]] = t
            kept.append(c)
        pool = kept
        stage = f"region:{region}" + (f"+확장({'+'.join(exp_to)})" if exp_to else "")
        trace["funnel"].append({"stage": stage, "n": len(pool)})

    # -- 배제 필터: 해당 태그 보유 카드 제거 (하드 배제 -- 어떤 경우에도 완화 없음) --
    excl = list(intent.get("배제") or [])
    if excl:
        pool = [c for c in pool if not any(t in (c.get("tags") or []) for t in excl)]
        trace["funnel"].append({"stage": "exclude:" + "+".join(excl), "n": len(pool)})

    # -- 하드 필터: 전부 보유한 카드만 (완화 없음 -- 주차 필수인 사람 신뢰의 문제) --
    hard = list(intent.get("하드") or [])
    if hard:
        pool = [c for c in pool if all(t in (c.get("tags") or []) for t in hard)]
        trace["funnel"].append({"stage": "hard:" + "+".join(hard), "n": len(pool)})

    # -- 소프트 점수 + 정렬 --
    soft = list(intent.get("소프트") or [])
    if soft:
        scored = []
        for c in pool:
            tags = set(c.get("tags") or [])
            hits = sum(1 for t in soft if t in tags)
            if hits > 0:  # 태그충족 0은 후보 제외 -- 0건이어야 relax 사다리가 발동
                scored.append((c, hits))
        trace["funnel"].append({"stage": "tags:" + "+".join(soft), "n": len(scored)})
        scored.sort(key=lambda cs: (tier_of.get(cs[0]["name"], 0), -cs[1],
                                    -cs[0].get("bloggers", 0), -_reaction(cs[0]),
                                    cs[0]["name"]))
        trace["results"] = [_result_entry(c, hits) for c, hits in scored[:k]]
    else:
        # 브라우즈: 빈 조건은 다수결(고유 블로거 수) 정렬 (server.py:408~427 미러)
        if kind == "브라우즈":
            k = max(k, 12)  # server.py:410 미러 -- 브라우즈는 넉넉히
        pool = sorted(pool, key=lambda c: (tier_of.get(c["name"], 0),
                                           -c.get("bloggers", 0),
                                           -c.get("mention_count", 0), c["name"]))
        trace["results"] = [_result_entry(c, 0) for c in pool[:k]]
    return trace


if __name__ == "__main__":
    # 가벼운 자가 점검 (관통 스모크는 app/relax.py __main__에 통합)
    t = retrieve({"query": "애월 오션뷰",
                  "intent": {"유형": "조건검색", "지역": "애월", "하드": [], "소프트": ["오션뷰"], "배제": []},
                  "translation": [], "unresolved": [], "funnel": [], "relaxation": [],
                  "region_expanded": None, "results": []})
    print("[retrieve] 자가 점검: funnel =", [(s["stage"], s["n"]) for s in t["funnel"]],
          "/ results", len(t["results"]))
    assert t["results"], "애월+오션뷰가 0건일 리 없음"
    ns = [s["n"] for s in t["funnel"]]
    assert all(a >= b for a, b in zip(ns, ns[1:])), "funnel 단계별 n이 증가함 (필터 누수)"
    print("[retrieve] 자가 점검 통과")
