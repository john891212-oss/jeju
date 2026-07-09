# -*- coding: utf-8 -*-
"""
W2-4 관통 검증 -- 시나리오 8종 통과표 (W2-4_지침.md 79~85행 / 소유: 검증)

역할:
  run_pipeline(query) = route_query -> retrieve -> (0건이면 relax) -> synthesize
  순수 함수 체인. W5 LangGraph 배선의 청사진 -- 각 단계는 TraceState(plain dict)를
  받아 자기 몫만 채워 반환하고, 이 파일의 체인 순서가 그대로 그래프 간선이 된다.
  (W5 그래프 파일은 여기서 만들지 않는다 -- 지침 93행)

시나리오 8종 (지침 81~84행):
  (1) 조회        -- "카페한라 어때". 단, cards.json 실존 확인 후 없으면 실존명 대체
                    (실측: 정확명 "카페한라"는 없음 -> "카페한라산"으로 대체, 통과표 명기)
  (2) 브라우즈    -- "애월 카페"
  (3) 조건 정상   -- "애월 오션뷰"
  (4) 어휘 간극   -- "노을 맛집" -> translation에 노을/exact
  (5) 0건->소프트 -- "우도 산방산뷰 카페" (우도x산방산뷰 = 실데이터 0건, 프로브 확인)
  (6) 0건->지역   -- "한경 노키즈존 카페" (한경x노키즈존 0건 -> 인접 한림3/대정0)
  (7) 완전 0건    -- "우도에서 강아지랑 갈 수 있는 노키즈존 카페"
                    (우도x[애견동반+노키즈존] 0건, 인접 성산의 노키즈존 2곳 모두
                     애견동반 없음 -> 사다리 끝까지 진짜 0건, 프로브 확인)
  (8) 미해석      -- "물멍하기 좋은 카페" -> unresolved + answer 언급

각 시나리오의 TraceState 전문은 eval/trace_샘플/시나리오N_설명.json 으로 저장
(ensure_ascii=False, indent=2, utf-8) -- /trace 응답과 발표 데모 재료.

사용 (실 LLM 호출 포함 -- router 1회 + synthesize 1회씩, .env OPENAI_KEY):
  python eval/w234_검증.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # server.py:26 ROOT 패턴
sys.path.insert(0, ROOT)  # 어디서 실행하든 app 패키지를 찾도록

from app.router import route_query                       # noqa: E402
from app.retrieve import retrieve, CARDS, ALIAS2CANON    # noqa: E402
from app.relax import relax                              # noqa: E402
from app.synthesize import synthesize, load_cards_by_name  # noqa: E402

TRACE_DIR = os.path.join(ROOT, "eval", "trace_샘플")
_CARDS_BY_NAME = load_cards_by_name()  # 8회 재로드 방지 -- 1회 로드 재사용


# ======================================================================
# run_pipeline -- W5 배선 청사진 (지침 93행: 세 모듈 전부 TraceState 순수 함수)
# ======================================================================
def run_pipeline(query, k=8):
    """자연어 질의 -> 완성된 TraceState (answer 포함).

    간선: router --> retrieve --(results 0건이고 조회 아님)--> relax --> synthesize
    - retrieve는 3유형(조회/브라우즈/조건검색) 전부 담당 (조회는 폐업 안내 포함).
    - relax 발동 조건은 "후보 0건일 때만" (지침 49행). 조회 0건은 완화할 조건이
      없으므로 건너뛴다 (relax 내부 가드와 동일 -- 간선을 명시해 W5 조건 간선 문서화).
    - 이번 스코프는 태그 경로만 -- 임베딩 폴백은 W5 통합 시 server.py 기존 경로 담당.
    """
    trace = route_query(query, k=k)
    kk = trace.get("k", k)
    trace = retrieve(trace, k=kk)
    if not trace["results"] and trace["intent"].get("유형") != "조회":
        trace = relax(trace, k=kk)
    trace = synthesize(trace, _CARDS_BY_NAME)
    return trace


# ======================================================================
# 헬퍼
# ======================================================================
def _squash(s):
    """공백 제거 비교 (synthesize의 규칙 2 판정과 같은 잣대)."""
    return re.sub(r"\s+", "", s or "")


def _answer_text(trace):
    a = trace.get("answer") or {}
    return " ".join([a.get("intro", ""),
                     " ".join((a.get("reasons") or {}).values()),
                     a.get("notice", "")])


def _tags_of(spot_name):
    c = CARDS.get(spot_name) or {}
    return c.get("tags") or []


def _bucket_of(spot_name):
    c = CARDS.get(spot_name) or {}
    return c.get("region_bucket")


def _norm_name(s):
    return re.sub(r"[^\w가-힣]", "", (s or "").lower())


def _resolve_s1_name():
    """지침 (1): '카페한라' 실존 확인 (이름+별칭, 정규화 완전 일치).
    없으면 그 문자열을 포함하는 실존 카페명으로 대체하고 (대체 여부, 정본명) 반환."""
    want = _norm_name("카페한라")
    for alias, canon in ALIAS2CANON.items():
        if _norm_name(alias) == want:
            return False, canon  # 실존 -- 대체 불필요
    cands = sorted(canon for alias, canon in ALIAS2CANON.items()
                   if want in _norm_name(alias))
    if not cands:
        raise RuntimeError("대체 후보조차 없음 -- 시나리오 1 질의를 다시 정해야 함")
    return True, cands[0]  # 실측: '카페한라산' (서빙, 구좌)


# ======================================================================
# 시나리오 정의 -- 각 항목: (파일 슬러그, 질의, 기대 요약, 체크 함수)
# 체크 함수는 [(라벨, bool), ...] 반환. 전부 True면 통과.
# ======================================================================
_S1_SUBST, _S1_NAME = _resolve_s1_name()


def _chk1(t):
    i = t["intent"]
    names = [r["spot_name"] for r in t["results"]]
    return [
        ("유형=조회 (이름 레이어, LLM 우회)", i["유형"] == "조회" and t["router_method"] == "name"),
        (f"results에 {_S1_NAME}", _S1_NAME in names),
        ("answer.reasons에 해당 카페", bool(((t.get("answer") or {}).get("reasons") or {}).get(_S1_NAME, "").strip())),
        ("honest_zero 아님", (t.get("answer") or {}).get("honest_zero") is False),
    ]


def _chk2(t):
    i = t["intent"]
    return [
        ("유형=브라우즈", i["유형"] == "브라우즈"),
        ("지역=애월", i["지역"] == "애월"),
        ("결과 12건 (브라우즈 k 부스트)", len(t["results"]) == 12),
        ("결과 전원 애월 버킷", all(_bucket_of(r["spot_name"]) == "애월" for r in t["results"])),
        ("answer.intro 생성", bool(((t.get("answer") or {}).get("intro") or "").strip())),
    ]


def _chk3(t):
    i = t["intent"]
    return [
        ("유형=조건검색", i["유형"] == "조건검색"),
        ("지역=애월", i["지역"] == "애월"),
        ("소프트에 오션뷰", "오션뷰" in i["소프트"]),
        ("결과>0", len(t["results"]) > 0),
        ("결과 전원 오션뷰 태그 보유", all("오션뷰" in _tags_of(r["spot_name"]) for r in t["results"])),
        ("완화 미발동", not t["relaxation"] and t["region_expanded"] is None),
    ]


def _chk4(t):
    trans = t["translation"]
    return [
        ("translation에 노을/exact", any(x.get("tag") == "노을" and x.get("method") == "exact" for x in trans)),
        ("소프트에 노을", "노을" in t["intent"]["소프트"]),
        ("결과>0", len(t["results"]) > 0),
        ("결과 전원 노을 태그 보유", all("노을" in _tags_of(r["spot_name"]) for r in t["results"])),
    ]


def _chk5(t):
    drops = [r for r in t["relaxation"] if r["action"] == "soft_drop"]
    label = "산방산뷰 조건을 빼고 찾았어요"
    return [
        ("soft_drop 발동 (산방산뷰)", any(r["condition"] == "산방산뷰" for r in drops)),
        ("relaxation label 존재", any(r.get("label") == label for r in drops)),
        ("완화 후 결과>0", len(t["results"]) > 0),
        ("결과 전원 우도 버킷", all(_bucket_of(r["spot_name"]) == "우도" for r in t["results"])),
        ("answer가 label 언급 (규칙 2)", _squash(label) in _squash(_answer_text(t))),
    ]


def _chk6(t):
    label = "한림, 대정까지 넓혀서 찾았어요"
    exps = [r for r in t["relaxation"] if r["action"] == "region_expand"]
    return [
        ("하드에 노키즈존", "노키즈존" in t["intent"]["하드"]),
        ("region_expand label 존재", any(r.get("label") == label for r in exps)),
        ("region_expanded 기록", t["region_expanded"] == {"from": "한경", "to": ["한림", "대정"]}),
        ("확장 후 결과>0", len(t["results"]) > 0),
        ("하드 보존: 결과 전원 노키즈존", all("노키즈존" in _tags_of(r["spot_name"]) for r in t["results"])),
        ("결과 전원 인접 버킷 소속", all(_bucket_of(r["spot_name"]) in ("한림", "대정") for r in t["results"])),
        ("answer가 label 언급 (규칙 2)", _squash(label) in _squash(_answer_text(t))),
    ]


def _chk7(t):
    i = t["intent"]
    a = t.get("answer") or {}
    return [
        ("하드 2종 (애견동반+노키즈존)", set(i["하드"]) == {"애견동반", "노키즈존"}),
        ("사다리 시도 흔적 (region_expand)", any(r["action"] == "region_expand" for r in t["relaxation"])),
        ("최종 결과 0건", t["results"] == []),
        ("honest_zero=True", a.get("honest_zero") is True),
        ("0건 선언이 먼저", (a.get("intro") or "").startswith("조건에 맞는 카페를 찾지 못했어요")),
        ("하드 불변 (완화 대상 아님)", all(r["condition"] not in ("애견동반", "노키즈존")
                                       for r in t["relaxation"])),
    ]


def _chk8(t):
    terms = [u.get("input") if isinstance(u, dict) else str(u) for u in t["unresolved"]]
    hit = [x for x in terms if "물멍" in x]
    text = _answer_text(t)
    return [
        ("unresolved에 물멍 계열", bool(hit)),
        ("translation에 unresolved 기록", any(x.get("method") == "unresolved" for x in t["translation"])),
        ("answer가 미해석 조건 언급", bool(hit) and any(_squash(x) in _squash(text) for x in hit)),
        ("answer에 해석 불가 안내", "해석하지 못했" in text),
    ]


SCENARIOS = [
    # (번호, 슬러그, 질의, 기대 요약, 체크)
    (1, "조회", f"{_S1_NAME} 어때",
     f"조회/{_S1_NAME} 카드 응답" + (" (대체: 카페한라 실존 안 함)" if _S1_SUBST else ""), _chk1),
    (2, "브라우즈", "애월 카페", "브라우즈/애월 12건", _chk2),
    (3, "조건정상", "애월 오션뷰", "조건검색/애월+오션뷰, 완화 없음", _chk3),
    (4, "어휘간극", "노을 맛집", "노을/exact 번역 + 결과", _chk4),
    (5, "소프트완화", "우도 산방산뷰 카페", "0건 -> soft_drop label + 우도 결과", _chk5),
    (6, "지역확장", "한경 노키즈존 카페", "0건 -> 한림/대정 확장 label + 결과", _chk6),
    (7, "완전0건", "우도에서 강아지랑 갈 수 있는 노키즈존 카페",
     "사다리 끝까지 0건 -> honest_zero", _chk7),
    (8, "미해석", "물멍하기 좋은 카페", "unresolved + answer 언급", _chk8),
]


# ======================================================================
# 실행
# ======================================================================
def main():
    os.makedirs(TRACE_DIR, exist_ok=True)
    rows = []
    n_pass = 0

    for num, slug, query, expect, chk in SCENARIOS:
        print()
        print(f"=== 시나리오 {num} [{slug}] {query}")
        try:
            trace = run_pipeline(query)
        except Exception as e:  # 삼킴 금지 -- 실패 시나리오로 기록하고 계속
            print(f"  [FAIL] 파이프라인 예외: {type(e).__name__}: {e}")
            rows.append((num, query, expect, f"예외 {type(e).__name__}", "FAIL"))
            continue

        checks = chk(trace)
        ok = all(c[1] for c in checks)
        for label, c_ok in checks:
            print(f"  [{'OK  ' if c_ok else 'FAIL'}] {label}")

        i = trace["intent"]
        actual = (f"{i['유형']}/{i['지역']} 결과{len(trace['results'])}건"
                  f" 완화{len(trace['relaxation'])}회"
                  + (" hz" if (trace.get('answer') or {}).get('honest_zero') else ""))
        print(f"  실측: {actual} / funnel: {[(s['stage'], s['n']) for s in trace['funnel']]}")

        path = os.path.join(TRACE_DIR, f"시나리오{num}_{slug}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)
        print(f"  trace 저장: {os.path.relpath(path, ROOT)}")

        if ok:
            n_pass += 1
        rows.append((num, query, expect, actual, "PASS" if ok else "FAIL"))

    # ---- 통과표 (ASCII 표) ----
    print()
    header = ("번호", "질의", "기대", "실측", "판정")
    table_rows = [header] + [(str(n), q, e, a, v) for n, q, e, a, v in rows]
    widths = [max(len(r[c]) for r in table_rows) for c in range(5)]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [sep]
    for ri, r in enumerate(table_rows):
        lines.append("| " + " | ".join(r[c].ljust(widths[c]) for c in range(5)) + " |")
        if ri == 0:
            lines.append(sep)
    lines.append(sep)
    table = "\n".join(lines)
    print(table)
    print()
    print(f"PASSED {n_pass}/{len(SCENARIOS)}")
    return n_pass, table


if __name__ == "__main__":
    n_pass, _ = main()
    if n_pass < len(SCENARIOS):
        raise SystemExit(1)
