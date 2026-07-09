# -*- coding: utf-8 -*-
"""
W4 종합 (W2-4_지침.md W4절) -- TraceState -> 근거 인용 추천문 (trace["answer"] 채움).

계약:
  synthesize(trace, cards_by_name=None) -> trace
  - 입력: TraceState (intent/translation/unresolved/relaxation/results 채워진 상태)
  - 출력: trace["answer"] = {"intro": str, "reasons": {카페명: str}, "notice": str,
                             "honest_zero": bool, "fallback": bool}
          trace["quote_violations"] = [{"where", "quote", "sentence"}, ...]
  - LLM(gpt-5-mini) 호출은 results가 1건 이상일 때 정확히 1회. 0건이면 0회.
  - trace dict를 받아 자기 몫만 채워 반환하는 순수 함수 형태 -- W5 LangGraph 배선 대비
    (지침 93행). 클래스 없음, plain dict만.

세 규칙 (지침 69~75행 -- 전부 코드로 강제, 프롬프트만 믿지 않는다):
  1. 인용 검증 (확정 결정 26 패턴): 출력의 따옴표 스팬(「」, 굽은/곧은 따옴표)은
     해당 카드의 summary / caution 각 항목 / reaction_hint / 태그 각각 --
     즉 "어느 한 part"의 부분문자열이어야 한다.
     (2026-07-09 리뷰 확정 결함 수정: 이전의 연결 문자열 대조는 필드 경계,
      카드 경계를 가로지르는 짜깁기 인용 -- 예: summary 끝+caution 시작 --
      을 통과시켰다. part 리스트 대조로 3종 우회 전부 차단.)
     실패 인용이 든 문장은 제거하고 trace["quote_violations"]에 기록.
     (번역/완화 안내용 인용 -- 질의 조각, 조건명, label 원문 -- 은 허용 목록으로 통과)
     알려진 한계 (인용 검증은 "존재 증명"이지 "맥락 증명"이 아님 -- 리뷰 합의 문서화):
     - 단일 part 안의 부분문자열이면 통과하므로, 부정어 반전("불편하다"의 원문에서
       "편하다는..." 만 인용)과 파편 인용("주차가" 두 글자만 인용)은 막지 못한다.
     - 따옴표 없이 서술한 날조 문장은 검증 범위 밖 (방어는 시스템 프롬프트 층뿐).
  2. 번역/완화 로그 인용 강제: relaxation.label과 unresolved input이 출력 어디에도
     없으면 코드가 결정적 문장을 notice에 덧붙인다.
  3. 0건 정직: results가 비면 LLM 호출 없이 코드 템플릿 --
     "조건에 맞는 카페를 찾지 못했어요"를 먼저, relaxation label과 함께 대안 안내.
     answer["honest_zero"]=True.

확정 결정 25: 인기 수치(bloggers/mention_count/rating_avg/rating_count 등)는
  LLM 입력 금지 -- 화이트리스트 dict를 새로 조립해 전달 (원본 카드 통째 전달 금지).
  build_llm_payload()가 금지 키를 재귀 검사해 위반 시 즉시 예외 (조용한 위반 금지).

LLM 실패 무해: 빈 응답/파싱 실패/키 없음 -> 폴백 = 코드 조립 답변
  (server.py match_info 스타일 -- 지역/태그 매칭 사실만 나열, 지어낼 수 없는 이유만).

사용 (스모크 -- 실 LLM 1회 포함, .env OPENAI_KEY 필요):
  python app/synthesize.py
"""
import json
import os
import re
import time

from openai import OpenAI, RateLimitError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS_PATH = os.path.join(ROOT, "data", "processed", "cards.json")
MAX_CARDS = 8  # 지침 68행: 입력 카드 5~8장 -- 상한 8


# ---- .env 로딩: server.py:39~46 미러 -- 정본은 server.py (환경변수 > .env) ----
def _load_env():
    env = {}
    envfile = os.path.join(ROOT, ".env")
    if os.path.exists(envfile):
        for line in open(envfile, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    if os.environ.get("OPENAI_KEY"):
        env["OPENAI_KEY"] = os.environ["OPENAI_KEY"]
    return env


_client = None


def _get_client():
    """지연 생성 -- 0건 경로/단위 테스트는 키 없이도 돌아야 한다."""
    global _client
    if _client is None:
        key = _load_env().get("OPENAI_KEY")
        if not key:
            raise RuntimeError("OPENAI_KEY 없음 -- 로컬은 .env, 배포는 환경변수로 주입하세요")
        _client = OpenAI(api_key=key)
    return _client


# ---- 카드 원본 로드: results.spot_name -> 카드 전문 (aliases 변형도 수용) ----
def load_cards_by_name():
    by_name = {}
    for c in json.load(open(CARDS_PATH, encoding="utf-8")):
        by_name[c["name"]] = c
        for a in c.get("aliases", []):
            by_name.setdefault(a, c)
    return by_name


# ---- 결정 25: LLM 입력 화이트리스트 -- 새 dict 조립 (원본 통째 전달 금지) ----
# 허용 필드: name, region(fine 우선), category, tags, summary, caution,
#            hours_hint, reaction_hint, closed. 수치/식별자/링크는 어떤 것도 금지.
def whitelist_card(card):
    caution = card.get("caution") or []
    if isinstance(caution, str):
        caution = [caution]
    return {
        "name": card.get("name", ""),
        "region": card.get("region_fine") or card.get("region_bucket") or "",
        "category": card.get("category", ""),
        "tags": list(card.get("tags") or []),
        "summary": card.get("summary", "") or "",
        "caution": list(caution),
        "hours_hint": card.get("hours_hint", "") or "",
        "reaction_hint": card.get("reaction_hint", "") or "",
        "closed": bool(card.get("closed", False)),
    }


_FORBIDDEN_KEYS = {"bloggers", "mention_count", "rating_avg", "rating_count",
                   "video_ids", "blog_links", "place_id", "lat", "lng", "address",
                   "review_tone", "reaction_tone", "aliases", "판정"}


def _check_no_forbidden(obj, path="payload"):
    """결정 25 가드 -- LLM 입력에 금지 키가 섞이면 즉시 예외 (프롬프트만 믿지 않는다)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _FORBIDDEN_KEYS:
                raise ValueError(f"결정 25 위반: LLM 입력에 금지 필드 '{k}' ({path})")
            _check_no_forbidden(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_no_forbidden(v, f"{path}[{i}]")


# ---- 규칙 1: 인용 검증 (확정 결정 26 패턴) ----
# 검증 원문 = 화이트리스트 카드의 summary / caution 각 항목 / reaction_hint / 태그 각각을
# "part 리스트"로 유지한다. 연결 문자열로 합치지 말 것 -- 합치면 필드/카드 경계가
# 사라져 짜깁기 인용(「곳이에요 주차가」류)이 검증을 통과한다 (2026-07-09 리뷰 실증).
def evidence_parts(wcard):
    parts = [wcard.get("summary", "")]
    parts.extend(wcard.get("caution") or [])
    parts.append(wcard.get("reaction_hint", ""))
    parts.extend(wcard.get("tags") or [])
    return [p for p in parts if p]


# 따옴표 스팬: 「」 / 굽은 큰따옴표 / 굽은 작은따옴표 / 곧은 큰따옴표 / 곧은 작은따옴표
_QUOTE_RE = re.compile(r"「([^」]{2,})」|“([^”]{2,})”|‘([^’]{2,})’"
                       r"|\"([^\"]{2,})\"|'([^']{2,})'")
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def _squash(s):
    """공백만 제거한 비교용 문자열 -- LLM의 사소한 공백 변형은 내용 동일로 본다."""
    return re.sub(r"\s+", "", s or "")


def _quote_ok(span, parts, allowed_terms):
    """span이 '어느 한 part'의 부분문자열일 때만 통과 (parts = evidence_parts 반환값).

    part별 개별 대조가 핵심 -- 전 part를 이어 붙인 문자열에 대조하면 경계를
    가로지르는 짜깁기 인용이 통과한다 (리뷰 실증 3종: 필드 경계/카드 경계/태그 나열).
    """
    sq = _squash(span)
    if not sq:
        return True
    for p in parts:
        if span in p or sq in _squash(p):
            return True
    # 카드 원문은 아니지만 정당한 인용: 질의 조각/조건명/완화 label 등 (허용 목록)
    return any(sq == _squash(t) for t in allowed_terms)


def _strip_bad_quotes(text, parts, allowed_terms, violations, where):
    """실패 인용이 든 '문장'을 제거하고 violations에 기록. 나머지는 그대로 잇는다."""
    if not text:
        return text or ""
    kept = []
    for sent in _SENT_SPLIT.split(text):
        if not sent or not sent.strip():
            continue
        bad = None
        for m in _QUOTE_RE.finditer(sent):
            span = next(g for g in m.groups() if g is not None)
            if not _quote_ok(span, parts, allowed_terms):
                bad = span
                break
        if bad is None:
            kept.append(sent.strip())
        else:
            violations.append({"where": where, "quote": bad, "sentence": sent.strip()})
            print(f"[synthesize] 인용 검증 실패({where}): 원문에 없는 인용 -> 문장 제거", flush=True)
    return " ".join(kept)


def _allowed_terms(trace, names):
    """카드 원문이 아니어도 따옴표 인용이 정당한 어휘 -- 질의/조건/번역/완화 문맥."""
    terms = {trace.get("query", "")}
    intent = trace.get("intent") or {}
    for k in ("하드", "소프트", "배제"):
        terms.update(intent.get(k) or [])
    if intent.get("지역"):
        terms.add(intent["지역"])
    for t in trace.get("translation") or []:
        terms.add(t.get("input") or "")
        terms.add(t.get("tag") or "")
        if t.get("tag") and t.get("input"):
            # payload의 번역_해석 줄을 통째로 인용하는 것도 정당 (실측: notice에서 발생)
            terms.add(_trans_line(t))
    for u in trace.get("unresolved") or []:
        terms.add(u.get("input") if isinstance(u, dict) else str(u))
    for r in trace.get("relaxation") or []:
        terms.add(r.get("condition") or "")
        terms.add(r.get("label") or "")
    if trace.get("region_expanded"):
        terms.add(str(trace["region_expanded"]))
    terms.update(names)
    return {t for t in terms if t}


# ---- 규칙 2: 번역/완화 로그 인용 강제 (코드가 확인, 없으면 코드가 덧붙임) ----
def _unresolved_inputs(trace):
    out = []
    for u in trace.get("unresolved") or []:
        term = u.get("input") if isinstance(u, dict) else str(u)
        if term:
            out.append(term)
    return out


def _enforce_log_mentions(trace, answer):
    joined = _squash(answer.get("intro", "")
                     + " ".join(answer.get("reasons", {}).values())
                     + answer.get("notice", ""))
    add = []
    for r in trace.get("relaxation") or []:
        label = r.get("label") or ""
        if label and _squash(label) not in joined:
            add.append(label if label.endswith((".", "!", "?")) else label + ".")
    for term in _unresolved_inputs(trace):
        if _squash(term) not in joined:
            add.append(f"'{term}' 조건은 해석하지 못했어요.")
    if add:
        print(f"[synthesize] 규칙 2 강제: notice에 {len(add)}건 덧붙임", flush=True)
        answer["notice"] = (answer.get("notice", "") + " " + " ".join(add)).strip()


# ---- LLM 호출 (gpt-5-mini) -- naver_refine.py:116~131 패턴 미러 ----
_SYSTEM = """너는 제주 카페 추천 답변을 쓰는 도우미다. 제공된 카드(cards) 정보만 근거로 쓴다.

규칙:
1. 카드에 없는 사실을 지어내지 마라. 따옴표(「」)로 인용할 때는 반드시 그 카드의
   summary/caution/reaction_hint/tags에 있는 문구를 글자 그대로 옮겨라.
   원문에 없는 문장을 따옴표로 감싸면 그 문장은 코드가 삭제한다.
2. 번역_해석("'노을 맛집'은 '노을' 태그로 해석"), 완화_안내문(원문 그대로),
   미해석_조건("'OO' 조건은 해석하지 못했어요")을 답변에 반드시 언급하라.
3. 방문자 수, 언급 수, 평점 같은 수치는 절대 말하지 마라 (입력에도 없다).
4. closed가 true인 카드는 폐업 안내를 먼저 하라.

출력은 JSON 하나:
{"intro": "질의 해석 요약과 추천 개요 1~3문장",
 "reasons": {"카페명": "그 카드 근거를 「인용」한 추천 이유 1~2문장"},
 "notice": "완화/미해석/주의 안내 (없으면 빈 문자열)"}
reasons의 키는 제공된 카드의 name을 글자 그대로 쓴다. 말투는 친근한 존댓말."""


def _trans_line(t):
    """번역 컨텍스트 한 줄 -- payload와 인용 허용 목록이 같은 문자열을 쓰도록 단일화."""
    return f"'{t['input']}' 은(는) '{t['tag']}' 태그로 해석 ({t.get('method', '')})"


def build_llm_payload(trace, wcards):
    """LLM user 입력 조립 -- 번역/완화 컨텍스트 요약 + 화이트리스트 카드만."""
    intent = trace.get("intent") or {}
    trans_lines = [_trans_line(t) for t in trace.get("translation") or []
                   if t.get("tag") and t.get("input")]
    labels = [r.get("label") or "" for r in trace.get("relaxation") or [] if r.get("label")]
    payload = {
        "질의": trace.get("query", ""),
        "의도": {"유형": intent.get("유형", ""), "지역": intent.get("지역"),
                 "하드": intent.get("하드") or [], "소프트": intent.get("소프트") or [],
                 "배제": intent.get("배제") or []},
        "번역_해석": trans_lines,
        "완화_안내문": labels,
        "미해석_조건": _unresolved_inputs(trace),
        "cards": wcards,
    }
    _check_no_forbidden(payload)  # 결정 25 -- 코드 레벨 강제
    return payload


def _call_llm(system, user, max_retry=5):
    """실패 무해: 어떤 실패든 경고 출력 후 None -> 호출부가 폴백 답변 조립."""
    try:
        client = _get_client()
    except RuntimeError as e:
        print(f"[synthesize] 경고: {e} -> 폴백 답변", flush=True)
        return None
    for i in range(max_retry):
        try:
            resp = client.chat.completions.create(
                model="gpt-5-mini",
                response_format={"type": "json_object"},
                max_completion_tokens=4000,
                reasoning_effort="minimal",  # 빈 응답 방지 실측 함정 (naver_refine.py)
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
            content = resp.choices[0].message.content or ""
            if not content.strip():
                print(f"[synthesize] 경고: 빈 응답 finish={resp.choices[0].finish_reason} -> 폴백", flush=True)
                return None
            return json.loads(content)
        except RateLimitError:
            print(f"[synthesize] RateLimit -- {2 ** i}초 대기 후 재시도", flush=True)
            time.sleep(2 ** i)
        except json.JSONDecodeError as e:
            print(f"[synthesize] 경고: JSON 파싱 실패 {e} -> 폴백", flush=True)
            return None
        except Exception as e:  # 실패 무해 -- 단, 삼키지 않고 경고는 반드시 출력
            print(f"[synthesize] 경고: LLM 호출 실패 {type(e).__name__}: {e} -> 폴백", flush=True)
            return None
    print("[synthesize] 경고: 재시도 초과 -> 폴백", flush=True)
    return None


# ---- 폴백/보충: 지어낼 수 없는 사실만 나열 (server.py match_info 스타일) ----
def _fact_reason(trace, wcard):
    wanted = set()
    for t in trace.get("translation") or []:
        if t.get("tag"):
            wanted.add(t["tag"])
    intent = trace.get("intent") or {}
    wanted.update(intent.get("하드") or [])
    wanted.update(intent.get("소프트") or [])
    hits = [t for t in wcard.get("tags") or [] if t in wanted]
    bits = []
    if wcard.get("region"):
        bits.append(f"{wcard['region']} 위치")
    if hits:
        bits.append("태그 일치: " + ", ".join(hits[:4]))
    if not bits:
        bits.append("지역, 태그 조건과 맞는 후보예요")
    return " / ".join(bits)


def _fallback_answer(trace, wcards):
    reasons = {w["name"]: _fact_reason(trace, w) for w in wcards}
    intro = (f"'{trace.get('query', '')}' 조건으로 {len(wcards)}곳을 찾았어요. "
             "(답변 생성이 잠시 어려워 매칭 사실만 표시합니다)")
    return {"intro": intro, "reasons": reasons, "notice": "",
            "honest_zero": False, "fallback": True}


# ---- 규칙 3: 0건 정직 -- LLM 호출 없이 코드 템플릿 ----
def _zero_answer(trace):
    labels = [r.get("label") or "" for r in trace.get("relaxation") or [] if r.get("label")]
    notice = []
    if labels:
        notice.append("이렇게도 찾아봤지만 결과가 없었어요: " + " / ".join(labels))
    for term in _unresolved_inputs(trace):
        notice.append(f"'{term}' 조건은 해석하지 못했어요.")
    notice.append("지역을 넓히거나 조건을 하나 줄여서 다시 검색해 보세요.")
    return {"intro": "조건에 맞는 카페를 찾지 못했어요.", "reasons": {},
            "notice": " ".join(notice), "honest_zero": True, "fallback": False}


# ---- 본체 ----
def synthesize(trace, cards_by_name=None):
    """TraceState -> trace["answer"] 채워 반환. LLM 1회 (0건이면 0회)."""
    trace.setdefault("quote_violations", [])
    results = trace.get("results") or []

    # 규칙 3: 0건은 0건 -- 정직한 실패가 컨셉
    if not results:
        trace["answer"] = _zero_answer(trace)
        return trace

    if cards_by_name is None:
        cards_by_name = load_cards_by_name()

    wcards, ev_by_name = [], {}
    for r in results[:MAX_CARDS]:
        name = r.get("spot_name") or ""
        card = cards_by_name.get(name)
        if card is None:
            print(f"[synthesize] 경고: cards.json에 없는 spot_name '{name}' -> 답변에서 제외", flush=True)
            continue
        w = whitelist_card(card)
        if w["name"] not in ev_by_name:
            wcards.append(w)
            ev_by_name[w["name"]] = evidence_parts(w)
    if not wcards:
        print("[synthesize] 경고: results 전부 카드 원문 미발견 -> 0건 템플릿 처리", flush=True)
        trace["answer"] = _zero_answer(trace)
        return trace

    payload = build_llm_payload(trace, wcards)
    raw = _call_llm(_SYSTEM, json.dumps(payload, ensure_ascii=False))

    if raw is None:
        answer = _fallback_answer(trace, wcards)
    else:
        answer = {"intro": str(raw.get("intro") or ""), "reasons": {},
                  "notice": str(raw.get("notice") or ""),
                  "honest_zero": False, "fallback": False}
        raw_reasons = raw.get("reasons") or {}
        if not isinstance(raw_reasons, dict):
            print("[synthesize] 경고: reasons가 dict 아님 -> 무시하고 사실 조립으로 대체", flush=True)
            raw_reasons = {}
        for k, v in raw_reasons.items():
            if k in ev_by_name:
                answer["reasons"][k] = str(v or "")
            else:  # 결과에 없는 카페명(환각 키)은 버림
                print(f"[synthesize] 경고: 결과에 없는 카페 '{k}' 이유는 버림", flush=True)

        # 규칙 1: 인용 검증 -- reasons는 해당 카드 part, intro/notice는 전 카드 part 합집합
        # (합집합도 part '리스트'다 -- 문자열로 잇지 말 것, 카드 간 경계 짜깁기 차단)
        allowed = _allowed_terms(trace, set(ev_by_name))
        union_parts = [p for parts in ev_by_name.values() for p in parts]
        viol = trace["quote_violations"]
        answer["intro"] = _strip_bad_quotes(answer["intro"], union_parts, allowed, viol, "intro")
        answer["notice"] = _strip_bad_quotes(answer["notice"], union_parts, allowed, viol, "notice")
        for name in list(answer["reasons"]):
            answer["reasons"][name] = _strip_bad_quotes(
                answer["reasons"][name], ev_by_name[name], allowed, viol, f"reasons:{name}")

        # 이유가 없거나 검증으로 전부 지워진 카드는 사실 조립으로 보충 (빈 이유 금지)
        for w in wcards:
            if not (answer["reasons"].get(w["name"]) or "").strip():
                answer["reasons"][w["name"]] = _fact_reason(trace, w)

    # 규칙 2: 번역/완화 로그 언급 강제 (LLM 경로, 폴백 경로 공통)
    _enforce_log_mentions(trace, answer)
    trace["answer"] = answer
    return trace


# ==== 스모크 (실행 지침: 실제 실행해 통과 확인) ====
if __name__ == "__main__":
    import random

    fails = []
    n_checks = [0]

    def check(name, ok, detail=""):
        n_checks[0] += 1
        mark = "OK  " if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not ok else ""))
        if not ok:
            fails.append(name)

    cards_by_name = load_cards_by_name()
    raw_cards = json.load(open(CARDS_PATH, encoding="utf-8"))
    # 서빙 근사(지침): closed==False 이고 판정=="유지" + 근거 텍스트가 실한 카드만 표본 후보
    serving = [c for c in raw_cards
               if not c.get("closed") and c.get("판정") == "유지"
               and c.get("region_bucket") == "애월"
               and len(c.get("summary") or "") >= 30 and c.get("reaction_hint")]
    sample = random.sample(serving, 3)  # 함정 계승: 표본은 random.sample
    print(f"[스모크] 표본 카드 3장: {[c['name'] for c in sample]}")

    def make_trace(cards):
        return {
            "query": "애월에서 노을 보기 좋은 조용한 카페",
            "intent": {"유형": "조건검색", "지역": "애월", "하드": [],
                       "소프트": ["노을", "조용함"], "배제": []},
            "translation": [
                {"input": "노을", "tag": "노을", "method": "exact", "score": 1.0},
                {"input": "물멍", "tag": None, "method": "unresolved", "score": 0.0},
            ],
            "unresolved": [{"input": "물멍"}],
            "funnel": [{"stage": "전체", "n": len(raw_cards)}],
            # 규칙 2 확인용: 완화 label을 일부러 주입 -- 출력에 반드시 나타나야 한다
            "relaxation": [{"action": "soft_drop", "condition": "조용함",
                            "label": "조용함 조건을 빼고 찾았어요"}],
            "region_expanded": None,
            "results": [{"spot_name": c["name"], "score_parts": {"태그충족": 1}} for c in cards],
        }

    # ---- (1) 인용 검증 단위 테스트: LLM 우회, 가짜 인용 직접 주입 ----
    print("[1] 인용 검증 단위 테스트 (LLM 우회)")
    w0 = whitelist_card(sample[0])
    ev0 = evidence_parts(w0)
    good = w0["summary"][:12]
    fake = "카드에 절대 없는 지어낸 인용문"
    text = f"이 집은 「{good}」 라는 소개가 있어요. 그런데 「{fake}」 라는 말도 있대요. 인용 없는 문장도 있어요."
    viol = []
    cleaned = _strip_bad_quotes(text, ev0, set(), viol, "unit")
    check("정상 인용 문장은 유지", good in cleaned)
    check("가짜 인용 문장은 제거", fake not in cleaned)
    check("인용 없는 문장은 유지", "인용 없는 문장도 있어요." in cleaned)
    check("violations 1건 기록", len(viol) == 1 and viol[0]["quote"] == fake,
          f"viol={viol}")

    # ---- (1b) 경계 짜깁기 회귀 테스트 (2026-07-09 리뷰 확정 결함 -- 실증 3종 차단) ----
    # 연결 문자열 대조 시절에는 아래 3종이 전부 '검증된 인용'으로 통과했다.
    print("[1b] 경계 짜깁기 회귀 테스트 (part 리스트 대조)")
    wA = {"name": "짜깁기테스트A", "summary": "노을이 정말 예쁘게 보이는 곳이에요",
          "caution": ["주차가 불편하다는 후기가 있어요"], "reaction_hint": "",
          "tags": ["노을", "조용함"]}
    wB = {"name": "짜깁기테스트B", "summary": "주차가 넓고 편해요",
          "caution": [], "reaction_hint": "", "tags": []}
    evA = evidence_parts(wA)
    union = evA + evidence_parts(wB)
    check("필드 경계 짜깁기 차단 (summary끝+caution시작)",
          not _quote_ok("곳이에요 주차가", evA, set()))
    check("태그 나열 짜깁기 차단 (태그 2개 이어붙임)",
          not _quote_ok("노을 조용함", evA, set()))
    check("카드 간 경계 짜깁기 차단 (A태그끝+B summary시작, union 대조)",
          not _quote_ok("조용함 주차가", union, set()))
    check("단일 part 내 정상 인용은 통과", _quote_ok("예쁘게 보이는", evA, set()))
    check("caution part 정상 인용은 통과",
          _quote_ok("주차가 불편하다는 후기", evA, set()))

    # ---- (2) 규칙 2 강제 단위 테스트: label/unresolved 없는 답변에 코드가 덧붙이는지 ----
    print("[2] 규칙 2 강제 단위 테스트 (LLM 우회)")
    fake_ans = {"intro": "테스트 답변", "reasons": {}, "notice": ""}
    _enforce_log_mentions(make_trace(sample), fake_ans)
    check("relaxation label 덧붙임", "조용함 조건을 빼고 찾았어요" in fake_ans["notice"])
    check("unresolved 안내 덧붙임", "'물멍' 조건은 해석하지 못했어요" in fake_ans["notice"])

    # ---- (3) 0건 경로: LLM 호출 0회 + honest_zero 템플릿 ----
    print("[3] 0건 정직 경로 (LLM 호출 없음 확인)")
    calls = {"n": 0}
    _real_call = _call_llm

    def _spy(*a, **k):
        calls["n"] += 1
        return _real_call(*a, **k)

    globals()["_call_llm"] = _spy
    zt = make_trace(sample)
    zt["results"] = []
    zt = synthesize(zt, cards_by_name)
    globals()["_call_llm"] = _real_call
    za = zt["answer"]
    check("LLM 호출 0회", calls["n"] == 0, f"calls={calls['n']}")
    check("honest_zero=True", za.get("honest_zero") is True)
    check("0건 선언이 먼저", za["intro"].startswith("조건에 맞는 카페를 찾지 못했어요"))
    check("완화 label 포함", "조용함 조건을 빼고 찾았어요" in za["notice"])
    check("unresolved 안내 포함", "물멍" in za["notice"])

    # ---- (4) 결정 25: LLM payload에 수치 필드 부재 + 가드 발동 ----
    print("[4] 결정 25 -- payload 수치 필드 차단")
    t25 = make_trace(sample)
    payload = build_llm_payload(t25, [whitelist_card(c) for c in sample])
    dumped = json.dumps(payload, ensure_ascii=False)
    leak = [k for k in ("bloggers", "mention_count", "rating_avg", "rating_count",
                        "place_id", "blog_links", "video_ids", "lat", "lng")
            if f'"{k}"' in dumped]
    check("금지 필드 부재", not leak, f"누출={leak}")
    try:
        _check_no_forbidden({"cards": [{"name": "x", "bloggers": 5}]})
        check("금지키 가드 발동", False, "예외가 나지 않음")
    except ValueError:
        check("금지키 가드 발동", True)

    # ---- (5) 실 LLM 1회: answer 생성 + 규칙 2 반영 + 호출 횟수 ----
    print("[5] 실 LLM 관통 (gpt-5-mini 1회)")
    calls["n"] = 0
    globals()["_call_llm"] = _spy
    lt = synthesize(make_trace(sample), cards_by_name)
    globals()["_call_llm"] = _real_call
    la = lt["answer"]
    all_text = la["intro"] + " ".join(la["reasons"].values()) + la["notice"]
    check("LLM 호출 정확히 1회", calls["n"] == 1, f"calls={calls['n']}")
    check("폴백 아님 (실 LLM 답변)", la.get("fallback") is False)
    check("intro 생성", bool(la["intro"].strip()))
    check("표본 카드 전부에 이유", all((la["reasons"].get(c["name"]) or "").strip() for c in sample),
          f"keys={list(la['reasons'])}")
    check("규칙 2: 완화 label 출력 반영", _squash("조용함 조건을 빼고 찾았어요") in _squash(all_text))
    check("규칙 2: unresolved 출력 반영", "물멍" in all_text)
    print("  --- 생성된 answer ---")
    print("  " + json.dumps(la, ensure_ascii=False, indent=2).replace("\n", "\n  "))
    if lt.get("quote_violations"):
        print(f"  quote_violations {len(lt['quote_violations'])}건 (검증기가 문장 제거함):")
        for v in lt["quote_violations"]:
            print(f"    - [{v['where']}] {v['quote'][:40]}")

    # ---- 결과 ----
    total = n_checks[0]
    if fails:
        print(f"[synthesize 스모크] 실패 {len(fails)}/{total}: {fails}")
        raise SystemExit(1)
    print(f"[synthesize 스모크] 전부 통과 ({total}/{total})")
