# -*- coding: utf-8 -*-
"""
W2 router -- 자연어 쿼리 -> TraceState (신규, W2-4_지침.md W2 절 / 소유: W2)

역할:
  route_query(query, k) -> 새 TraceState dict를 만들어
  query / intent / translation / unresolved 까지 채워 반환한다.
  (funnel/relaxation/results 는 빈 골격만 -- W3/W4 가 자기 몫을 채운다)

3분기 (확정 결정 21의 LLM 승계):
  1. 조회      -- 질의에 카페명이 있으면 LLM 호출 없이 결정적으로 확정
                 (식별자는 LLM 우회 -- HANDOFF 결정 5). intent.pinned 확장 필드.
  2. LLM 1회   -- gpt-5-mini 구조화 추출만: {"유형","지역","조건"}. 창의성 금지.
  3. 코드 후처리 -- 조건 라벨 번역(translate)·하드/소프트 분리(is_hard)·
                 배제(exclude_for 의미론)는 전부 코드가 한다 (지침 44행).

실패 무해 원칙 (지침 41행):
  LLM 예외·빈 응답 시 server.py 코드 3분기 미러로 폴백.
  어느 경로였는지 trace["router_method"] = "name|llm|fallback" 에 기록.

사용:
  python -m app.router      # 스모크 (실 LLM 호출 포함, .env OPENAI_KEY)
"""
import json
import os
import re
import time

from openai import OpenAI, RateLimitError

try:
    from app import tagdict  # 패키지 경로 실행 (python -m app.router, W5 배선)
except ImportError:
    import tagdict  # app/ 폴더 안에서 직접 실행

# W1 본판(app/translate.py) 완료 시 자동 교체 -- 그 전엔 스텁 (지침 (0)-3)
try:
    from app.translate import translate
except ImportError:
    try:
        from app.translate_stub import translate
    except ImportError:
        from translate_stub import translate  # app/ 폴더 안에서 직접 실행

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # server.py:38 패턴


# ==== .env 로딩 (server.py:42~53 미러 -- 정본은 server.py) ====
# 단, 키 없음이 치명상이 아님: router는 LLM 없이도 폴백으로 동작해야 한다 (실패 무해).
def _load_env():
    env = {}
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    if os.environ.get("OPENAI_KEY"):  # 환경변수가 있으면 우선 (배포)
        env["OPENAI_KEY"] = os.environ["OPENAI_KEY"]
    return env


_ENV = _load_env()
_CLIENT = None  # 지연 생성 -- import 시점에 키 없어도 모듈은 살아야 한다


def _get_client():
    global _CLIENT
    if _CLIENT is None:
        key = _ENV.get("OPENAI_KEY")
        if not key:
            print("[router] 경고: OPENAI_KEY 없음, LLM 없이 코드 폴백으로만 동작")
            return None
        _CLIENT = OpenAI(api_key=key)
    return _CLIENT


# ==== 텍스트 정규화 (server.py:63~67 _clean/_norm 미러 -- 정본은 server.py) ====
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s):
    return _TAG_RE.sub("", s or "").strip()


def _norm(s):
    s = _clean(s).split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())


# ==== 카드 정본 로드 (server.py:70~77 미러) ====
CARDS = {}        # 정본명 -> 카드
ALIAS2CANON = {}  # 모든 변형 -> 정본명
for _c in json.load(open(os.path.join(ROOT, "data", "processed", "cards.json"),
                         encoding="utf-8")):
    CARDS[_c["name"]] = _c
    ALIAS2CANON[_c["name"]] = _c["name"]
    for _a in _c.get("aliases", []):
        ALIAS2CANON[_a] = _c["name"]


# ==== 이름 매치 레이어 (server.py:94~134 미러 -- 정본은 server.py) ====
# 고유명사 조회는 임베딩·LLM의 직업이 아님 ("해지개" top10 전멸 실측 2026-07-08).
# 차이 1곳: 서빙 판별을 chroma(SERVING) 대신 카드 필드로 근사 --
#   판정=="유지"(서빙) 또는 closed(폐업 안내용)만 조회 사전에 올린다.
_NAME_STOP = {"카페", "커피", "제주", "제주도", "베이커리", "디저트", "브런치",
              "애월", "곽지", "한림", "협재", "함덕", "월정리", "세화", "김녕", "성산",
              "표선", "남원", "위미", "중문", "사계", "대정", "안덕", "우도", "구좌", "조천",
              "제주시내", "서귀포시내", "서귀포", "제주시", "월정"}


def _build_name_index():
    idx = {}  # 정규화 변형 -> 정본명
    for alias, canon in ALIAS2CANON.items():
        c = CARDS[canon]
        if c.get("판정") != "유지" and not c.get("closed"):
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
print(f"[router] 카드 {len(CARDS)}장, 이름 사전 {len(NAME_IDX)}건 (폐업 안내 포함)")


def name_lookup(q, limit=2):
    """질의에서 카페명 탐지 -> 정본명 목록. 긴 이름 우선.
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


# ==== 지역 라벨 2단 계층 (server.py:127 미러 -- 정본은 server.py) ====
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


# ==== 브라우즈 판별 + 조건 토큰 (server.py:161~182 미러 -- 정본은 server.py) ====
_BROWSE_STRIP = sorted(_NAME_STOP | {"추천", "여행", "가볼만한", "가볼만", "곳", "리스트",
                                     "목록", "투어", "베스트", "유명한", "유명"}, key=len, reverse=True)


def is_browse(q):
    r = _norm(q)
    for t in _BROWSE_STRIP:
        r = r.replace(t, "")
    return not r


def _terms(q):
    """질의에서 조건 토큰 추출 -- 지역·일반어 제외, 활용형 대비 축소형 포함."""
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


# ==== 배제 감지 (exclude_for 의미론 -- 태그사전이 정본, 지침 45행) ====
# "아이 동반" 맥락이면 노키즈존을 하드 배제. 오탐 2종 방어:
#   "아이스"(아이스아메리카노)의 "아이", "노키즈존"(어른 전용을 원하는 쿼리)의 "키즈".
_CHILD_RE = re.compile(r"아이(?!스)|아기|애기|유아|(?<!노)키즈")


def _detect_exclusions(query):
    out = []
    if "노키즈존" in tagdict.exclude_map() and _CHILD_RE.search(query):
        out.append("노키즈존")
    return out


# ==== 조건 라벨 -> 태그 번역 + 하드/소프트 분리 (전부 코드 -- LLM 판정 금지) ====
def _translate_label(label):
    """라벨 1개 번역. 완전 일치 실패 시 조사 낙하 어간([:-1], [:-2])도 시도
    (_terms의 활용형 대비 축소형과 같은 철학 -- "강아지랑" -> "강아지")."""
    rec = translate(label)
    if rec["tag"] is None:
        stems = ([label[:-1]] if len(label) >= 3 else []) + \
                ([label[:-2]] if len(label) >= 4 else [])
        for stem in stems:
            r2 = translate(stem)
            if r2["tag"] is not None:
                r2 = dict(r2)
                r2["input"] = label  # trace에는 사용자 표현 그대로 남긴다
                return r2
    return rec


def _classify_conditions(labels, trace):
    """조건 라벨들을 번역해 translation[]에 기록하고,
    해석된 태그를 is_hard()로 하드/소프트 분리, 미해석은 unresolved[]."""
    intent = trace["intent"]
    seen = set()
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        rec = _translate_label(label)
        trace["translation"].append(rec)
        if rec["tag"] is None:
            trace["unresolved"].append(label)
            continue
        bucket = "하드" if tagdict.is_hard(rec["tag"]) else "소프트"
        if rec["tag"] not in intent[bucket]:
            intent[bucket].append(rec["tag"])


# ==== LLM 구조화 추출 (호출 1회, 창의성 금지) ====
_SYSTEM = f"""제주 카페 검색 쿼리에서 검색 의도를 추출해 json으로만 답해.

## 출력 스키마
{{"유형": "브라우즈" 또는 "조건검색", "지역": 표준 라벨 문자열 또는 null, "조건": [문자열, ...]}}

## 규칙
- 창의성 금지. 쿼리에 적힌 것만 추출하고, 없는 조건이나 지역을 만들어내지 마.
- 유형: 지역명과 "카페/커피/추천/여행" 같은 일반어 외에 원하는 성질(뷰, 분위기, 메뉴,
  동반 조건, 설비 등)이 하나라도 있으면 "조건검색", 없으면 "브라우즈".
- 지역: 쿼리에 지역이 있으면 반드시 다음 표준 라벨 중 하나로만 답해.
  목록: {", ".join(REGIONS)}
  표기 변형 정규화: {", ".join(f"{a} -> {std}" for a, std in ALIAS.items())}
  목록 밖 지역이거나 지역 언급이 없으면 null. 새 라벨 발명 금지.
- 조건: 원하는 성질 각각을 사용자 표현을 살린 짧은 라벨로. 서술어는 떼되 뜻은 바꾸지 마.
  예: "노을 보이는" -> "노을", "조용한" -> "조용한",
      "강아지랑 갈 수 있는" -> "강아지랑", "아이랑 가기 좋은" -> "아이랑", "주차 되는" -> "주차".
  지역명과 카페/커피/추천 같은 일반어는 조건이 아니다."""


def _llm_intent(query, client=None, max_retry=3):
    """gpt-5-mini 1회 호출 -> dict 또는 None(실패 -> 호출부가 코드 폴백).
    호출 패턴은 pipeline/naver_refine.py refine():116~131 미러
    (max_completion_tokens=4000 + reasoning_effort="minimal" = 빈 응답 방지 실측 함정)."""
    client = client or _get_client()
    if client is None:
        return None
    for i in range(max_retry):
        try:
            resp = client.chat.completions.create(
                model="gpt-5-mini",
                response_format={"type": "json_object"},
                max_completion_tokens=4000,
                reasoning_effort="minimal",
                messages=[{"role": "system", "content": _SYSTEM},
                          {"role": "user", "content": query}],
            )
            content = resp.choices[0].message.content or ""
            if not content.strip():
                print(f"[router] 경고: LLM 빈 응답 (finish={resp.choices[0].finish_reason}), 코드 폴백")
                return None
            d = json.loads(content)
            if not isinstance(d, dict):
                print("[router] 경고: LLM 응답이 json 객체가 아님, 코드 폴백")
                return None
            return d
        except RateLimitError:
            print(f"[router] RateLimit, {2 ** i}초 대기 후 재시도 ({i + 1}/{max_retry})")
            time.sleep(2 ** i)
        except json.JSONDecodeError as e:
            print(f"[router] 경고: LLM 응답 파싱 실패 ({e}), 코드 폴백")
            return None
        except Exception as e:  # 인증·네트워크 등 -- 삼키지 않고 경고 후 폴백 (실패 무해)
            print(f"[router] 경고: LLM 호출 실패 ({type(e).__name__}), 코드 폴백")
            return None
    print("[router] 경고: RateLimit 재시도 초과, 코드 폴백")
    return None


# ==== 본체: route_query ====
def _new_trace(query, k):
    """TraceState 골격 (W2-4_지침.md 공통 계약). router는 자기 몫만 채운다."""
    return {
        "query": query,
        "intent": {"유형": None, "지역": None, "하드": [], "소프트": [], "배제": []},
        "translation": [],
        "unresolved": [],
        "funnel": [],          # W3 몫
        "relaxation": [],      # W3 몫
        "region_expanded": None,  # W3 몫
        "results": [],         # W3/W4 몫
        "k": k,                # 확장 필드: 다운스트림 검색 크기 (W3가 사용)
        "router_method": None,  # name|llm|fallback
    }


def route_query(query, k=8, _client=None):
    """자연어 쿼리 -> TraceState (intent/translation/unresolved까지 채움).

    _client: 테스트 전용 주입 지점 (예: 잘못된 키의 클라이언트로 폴백 경로 검증).
             평시엔 None -- .env OPENAI_KEY로 지연 생성한 모듈 클라이언트 사용.
    """
    trace = _new_trace(query, k)
    intent = trace["intent"]
    intent["배제"] = _detect_exclusions(query)

    # -- 1) 이름 조회: LLM보다 먼저, 결정적 (식별자는 LLM 우회 -- HANDOFF 결정 5) --
    pinned = name_lookup(query)
    if pinned:
        intent["유형"] = "조회"
        intent["pinned"] = pinned  # 확장 필드: 정본명 목록
        intent["지역"] = detect_region(query)  # 참고 정보 (조회 응답은 pinned가 결정)
        trace["router_method"] = "name"
        return trace

    # -- 2) LLM 1회: 구조화 추출만 --
    d = _llm_intent(query, client=_client)
    if d is not None:
        trace["router_method"] = "llm"
        # 지역 검증: 표준 라벨 밖이면 버리고 detect_region 폴백 (환각 지역 방어)
        region = d.get("지역")
        if isinstance(region, str):
            region = ALIAS.get(region.strip(), region.strip())
        if region not in REGIONS:
            if region:
                print(f"[router] 경고: LLM 지역 '{region}' 표준 라벨 밖, detect_region 폴백")
            region = detect_region(query)
        intent["지역"] = region

        labels = [s.strip() for s in (d.get("조건") or [])
                  if isinstance(s, str) and s.strip()]
        _classify_conditions(labels, trace)
        # 유형은 조건 유무로 코드가 확정 (LLM 라벨은 참고만 -- 조건 없는 조건검색은 성립 불가)
        intent["유형"] = "조건검색" if labels else "브라우즈"
        return trace

    # -- 3) 실패 무해 폴백: server.py 코드 3분기 미러 (이름은 위에서 이미 확인) --
    trace["router_method"] = "fallback"
    intent["지역"] = detect_region(query)
    if is_browse(query):
        intent["유형"] = "브라우즈"
        return trace
    intent["유형"] = "조건검색"
    labels = []
    for stems in _terms(query):
        # 지역 잔여 토큰 방어: 어간 중 하나라도 스톱워드면 조건이 아니다 ("애월에서" -> "애월")
        if any(_norm(s) in _NAME_STOP for s in stems):
            continue
        labels.append(stems[-1])  # 대표 라벨 = 원 토큰 (stems는 길이 오름차순)
    _classify_conditions(labels, trace)
    return trace


# ==== 스모크 (실 LLM 호출 포함 -- .env OPENAI_KEY) ====
if __name__ == "__main__":
    fails = []

    def _fmt(t):
        i = t["intent"]
        s = (f"method={t['router_method']} 유형={i['유형']} 지역={i['지역']} "
             f"하드={i['하드']} 소프트={i['소프트']} 배제={i['배제']} unresolved={t['unresolved']}")
        if "pinned" in i:
            s += f" pinned={i['pinned']}"
        return s

    def _run(name, query, checks, _client=None):
        t = route_query(query, _client=_client)
        print(f"\n[{name}] {query}")
        print("  " + _fmt(t))
        for label, ok in checks(t):
            print(f"  [{'OK  ' if ok else 'FAIL'}] {label}")
            if not ok:
                fails.append(f"{name}: {label}")
        return t

    _run("1 조건검색", "애월에서 노을 보이는 조용한 카페", lambda t: [
        ("method=llm", t["router_method"] == "llm"),
        ("유형=조건검색", t["intent"]["유형"] == "조건검색"),
        ("지역=애월", t["intent"]["지역"] == "애월"),
        ("소프트에 노을", "노을" in t["intent"]["소프트"]),
        ("소프트에 조용함", "조용함" in t["intent"]["소프트"]),
    ])

    _run("2 브라우즈", "함덕 카페 추천", lambda t: [
        ("method=llm", t["router_method"] == "llm"),
        ("유형=브라우즈", t["intent"]["유형"] == "브라우즈"),
        ("지역=함덕", t["intent"]["지역"] == "함덕"),
    ])

    _run("3 하드조건", "강아지랑 갈 수 있는 한림 카페", lambda t: [
        ("method=llm", t["router_method"] == "llm"),
        ("유형=조건검색", t["intent"]["유형"] == "조건검색"),
        ("지역=한림", t["intent"]["지역"] == "한림"),
        ("하드에 애견동반", "애견동반" in t["intent"]["하드"]),
    ])

    _run("4 배제", "아이랑 가기 좋은 카페", lambda t: [
        ("배제에 노키즈존", "노키즈존" in t["intent"]["배제"]),
    ])

    _run("5 조회", "프릳츠 어때", lambda t: [
        ("method=name (LLM 우회)", t["router_method"] == "name"),
        ("유형=조회", t["intent"]["유형"] == "조회"),
        ("pinned에 프릳츠 제주성산점", "프릳츠 제주성산점" in t["intent"].get("pinned", [])),
    ])

    # 폴백 경로: 잘못된 키의 클라이언트를 주입해 LLM을 강제로 죽인다 (인증 실패 -> 코드 3분기)
    _bad = OpenAI(api_key="sk-invalid-key-for-fallback-smoke")

    _run("6 폴백 조건검색", "애월에서 노을 보이는 조용한 카페", lambda t: [
        ("method=fallback", t["router_method"] == "fallback"),
        ("유형=조건검색", t["intent"]["유형"] == "조건검색"),
        ("지역=애월", t["intent"]["지역"] == "애월"),
        ("소프트에 노을", "노을" in t["intent"]["소프트"]),
        ("소프트에 조용함", "조용함" in t["intent"]["소프트"]),
    ], _client=_bad)

    _run("7 폴백 브라우즈", "함덕 카페 추천", lambda t: [
        ("method=fallback", t["router_method"] == "fallback"),
        ("유형=브라우즈", t["intent"]["유형"] == "브라우즈"),
        ("지역=함덕", t["intent"]["지역"] == "함덕"),
    ], _client=_bad)

    print()
    if fails:
        print(f"[router] 스모크 실패 {len(fails)}건: {fails}")
        raise SystemExit(1)
    print("[router] 스모크 전부 통과 (7 케이스)")
