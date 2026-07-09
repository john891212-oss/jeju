# -*- coding: utf-8 -*-
"""
W3 -- 완화 사다리 (LLM 호출 0회, W2-4 지침 47~64행)

역할:
  relax(trace, k=8) -> trace
  retrieve(app/retrieve.py) 결과가 0건일 때만 발동. 조건을 사다리 순서대로
  하나씩 포기하며 재검색하고, 모든 완화를 relaxation[]에 label과 함께 기록한다.
  이 label이 그대로 사용자에게 보인다 -- 설명가능성은 후처리가 아니라 데이터.

사다리 (순서 고정, 각 단마다 retrieve 재호출 1회, 처음으로 후보>0이면 멈춤):
  1. 소프트 제거 반복 -- translation score 낮은 것(해석 확신이 약한 조건)부터.
     소프트 완화는 누적된다 (한 번 뺀 조건을 다시 넣지 않는다).
  2. 인접 지역 확장 -- 세부지역 쿼리는 세부->버킷 승격이 첫 반칸, 그다음 인접 버킷.
  3. 끝. 하드조건과 배제조건은 어떤 경우에도 완화하지 않는다
     (주차 필수인 사람에게 주차 없는 곳을 주면 신뢰 끝 -- 정직한 0건이 정답).

구현 노트:
  - 각 단의 재검색은 작업 사본 trace로 프로브한다 -- 원 trace의 funnel에는
    요약 한 줄(relax:...)만 남겨 단계별 n 추이를 깨끗하게 유지 (W5 /trace 응답 대비).
  - retrieve의 소프트 의미론(태그충족>=1 필수)상 소프트가 남아있는 중간 단은
    이론상 계속 0건이고 전부 비운 마지막 단에서야 후보가 생긴다. 그래도 단마다
    프로브+기록하는 건 지침 명세(각 단계마다 재검색 1회)와 trace 정직성 때문.
"""
try:
    from app.retrieve import retrieve, CARDS, ALIAS, _label_to_bf  # 패키지 실행 (python -m app.relax)
except ImportError:
    from retrieve import retrieve, CARDS, ALIAS, _label_to_bf  # app/ 안 직접 실행

# ---- 인접 지역표 (버킷 기준) ----
# ⚠ 초안 -- 민옥 지리 검수 전. 한경 항목은 데이터에 버킷 실존(16장)하여 보완 추가
# (한림-대정 사이 서해안). 중문은 버킷이 아니라 서귀포시내의 세부지역이지만 지침 초안 유지.
# 중문이 확장 목적지로 나오면 _label_to_bf로 서귀포시내 버킷으로 해석한다.
ADJACENT = {"제주시내": ["애월", "조천"], "애월": ["제주시내", "한림"], "한림": ["애월", "대정"],
            "대정": ["한림", "안덕"], "안덕": ["대정", "중문"], "중문": ["안덕", "서귀포시내"],
            "서귀포시내": ["중문", "남원"], "남원": ["서귀포시내", "표선"], "표선": ["남원", "성산"],
            "성산": ["표선", "구좌", "우도"], "구좌": ["성산", "조천"], "조천": ["구좌", "제주시내"],
            "우도": ["성산"], "한경": ["한림", "대정"]}


def _probe(trace, softs, region_label, region_expanded, k):
    """작업 사본으로 retrieve 재호출 1회. 원 trace는 건드리지 않는다.

    반환: (후보 수 n = 사본 funnel 마지막 단계의 n, results 리스트).
    n은 k 절단 전의 후보 전수 -- "후보>0이면 멈춤" 판정은 이걸로 한다.
    """
    intent = dict(trace.get("intent") or {})
    intent["소프트"] = list(softs)
    if region_label is not None:
        intent["지역"] = region_label
    work = {"query": trace.get("query"), "intent": intent,
            "translation": trace.get("translation") or [],
            "unresolved": trace.get("unresolved") or [],
            "funnel": [], "relaxation": [], "region_expanded": region_expanded, "results": []}
    retrieve(work, k=k)
    n = work["funnel"][-1]["n"] if work["funnel"] else 0
    return n, work["results"]


def _pick_drop(softs, orig_order, translation):
    """다음에 제거할 소프트 1개 -- translation score 낮은 것부터.

    - score: translation[]에서 해당 태그의 최대 score. 기록이 없으면 0.0
      (확신 정보가 없는 조건 = 가장 약한 조건으로 취급, 먼저 제거).
    - 동률: 뒤에 말한 조건부터 제거 (앞에 말한 조건일수록 중요하다는 가정 --
      판단 지점, 민옥 확인 대상). 최종 동률은 태그명 사전순 (완전 결정론).
    """
    score = {}
    for t in translation:
        if t.get("tag"):
            s = t.get("score")
            score[t["tag"]] = max(score.get(t["tag"], 0.0), float(s if s is not None else 0.0))
    pos = {tag: i for i, tag in enumerate(orig_order)}
    return min(softs, key=lambda t: (score.get(t, 0.0), -pos.get(t, 0), t))


def relax(trace, k=8):
    """완화 사다리. results가 0건일 때만 발동, 아니면 그대로 반환.

    trace의 relaxation/funnel/region_expanded/results만 채운다.
    intent는 수정하지 않는다 -- 사용자의 원 의도는 보존하고, 완화는 relaxation[]이 정본.
    """
    trace.setdefault("relaxation", [])
    trace.setdefault("funnel", [])
    trace.setdefault("region_expanded", None)
    if trace.get("results"):
        return trace  # 발동 조건 미충족 -- 이미 후보가 있다

    intent = trace.get("intent") or {}
    if (intent.get("유형") or "조건검색") == "조회":
        return trace  # 이름 조회 0건은 완화할 조건이 없다 (W4가 "사전에 없음"을 정직 보고)

    # ---- 1단: 소프트 제거 반복 (score 낮은 것부터, 누적) ----
    orig_softs = list(intent.get("소프트") or [])
    softs = list(orig_softs)
    translation = trace.get("translation") or []
    while softs:
        drop = _pick_drop(softs, orig_softs, translation)
        softs.remove(drop)
        trace["relaxation"].append({"action": "soft_drop", "condition": drop,
                                    "label": f"{drop} 조건을 빼고 찾았어요"})
        n, results = _probe(trace, softs, None, None, k)
        trace["funnel"].append({"stage": f"relax:{drop} 제거", "n": n})
        if n > 0:
            trace["results"] = results
            return trace

    # ---- 2단: 인접 지역 확장 (1단의 소프트 제거는 누적된 상태로) ----
    region_raw = intent.get("지역")
    if region_raw:
        region = ALIAS.get(region_raw, region_raw)
        want_b, want_f = _label_to_bf(region)
        cur_label = region
        if want_f:
            # 반칸: 세부 -> 버킷 승격 (협재 -> 한림 전체)
            trace["relaxation"].append({"action": "region_expand", "condition": region,
                                        "label": f"{region} 밖 {want_b}까지 넓혀서 찾았어요"})
            n, results = _probe(trace, softs, want_b, None, k)
            trace["funnel"].append({"stage": f"relax:지역 {region}->{want_b}", "n": n})
            cur_label = want_b
            if n > 0:
                trace["region_expanded"] = {"from": region, "to": [want_b]}
                trace["results"] = results
                return trace
        # 온칸: 인접 버킷 확장 (중문 등 세부 라벨은 버킷으로 해석, 원 버킷 중복 제거)
        targets = []
        for t in ADJACENT.get(want_b, []):
            tb, _ = _label_to_bf(t)
            if tb != want_b and tb not in targets:
                targets.append(tb)
        if targets:
            exp = {"from": region, "to": targets}
            trace["relaxation"].append({"action": "region_expand", "condition": want_b,
                                        "label": ", ".join(targets) + "까지 넓혀서 찾았어요"})
            n, results = _probe(trace, softs, cur_label, exp, k)
            trace["funnel"].append({"stage": "relax:지역확장 " + "+".join(targets), "n": n})
            if n > 0:
                trace["region_expanded"] = exp
                trace["results"] = results
                return trace
        else:
            print(f"[relax] 경고: 인접표에 없는 버킷 '{want_b}' -- 지역 확장 불가")

    # ---- 3단: 끝 -- 하드/배제는 어떤 경우에도 완화하지 않는다. 정직한 0건. ----
    return trace


# ======================================================================
# 관통 스모크 (실제 cards.json) -- python -m app.relax
# ======================================================================
if __name__ == "__main__":
    import json as _json

    def _mk(query, kind, region, hard=None, soft=None, excl=None, name=None, translation=None):
        """TraceState 골격 생성 (router가 채워줄 부분을 스모크가 직접 채움)."""
        intent = {"유형": kind, "지역": region, "하드": hard or [],
                  "소프트": soft or [], "배제": excl or []}
        if name:
            intent["이름"] = name
        return {"query": query, "intent": intent, "translation": translation or [],
                "unresolved": [], "funnel": [], "relaxation": [],
                "region_expanded": None, "results": []}

    def _run(t, k=8):
        return relax(retrieve(t, k=k), k=k)

    fails = []

    def _check(label, ok, detail=""):
        print(f"  [{'OK  ' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
        if not ok:
            fails.append(label)

    # ---- S1. 정상 조건 (애월+오션뷰): 후보>0, funnel 단계별 n 감소, 완화 없음 ----
    print("[S1] 애월 + 소프트[오션뷰]")
    t1 = _run(_mk("애월 오션뷰 카페", "조건검색", "애월", soft=["오션뷰"],
                  translation=[{"input": "오션뷰", "tag": "오션뷰", "method": "exact", "score": 1.0}]))
    ns = [s["n"] for s in t1["funnel"]]
    print("      funnel:", [(s["stage"], s["n"]) for s in t1["funnel"]])
    _check("S1 후보>0", len(t1["results"]) > 0, f"{len(t1['results'])}건")
    _check("S1 funnel 단계별 n 비증가", all(a >= b for a, b in zip(ns, ns[1:])), str(ns))
    _check("S1 완화 미발동", not t1["relaxation"] and t1["region_expanded"] is None)
    _check("S1 결과 전원 오션뷰 보유",
           all("오션뷰" in (CARDS[r["spot_name"]].get("tags") or []) for r in t1["results"]))

    # ---- S2. 0건 -> 소프트 제거 (score 낮은 것부터): 우도 + [산방산뷰 1.0, 한라산뷰 0.6] ----
    print("[S2] 우도 + 소프트[산방산뷰(1.0), 한라산뷰(0.6)] -> soft_drop 사다리")
    t2 = _run(_mk("우도 산방산뷰 한라산 보이는 카페", "조건검색", "우도",
                  soft=["산방산뷰", "한라산뷰"],
                  translation=[{"input": "산방산뷰", "tag": "산방산뷰", "method": "exact", "score": 1.0},
                               {"input": "한라산 보이는", "tag": "한라산뷰", "method": "embedding", "score": 0.6}]))
    acts = [(r["action"], r["condition"]) for r in t2["relaxation"]]
    print("      relaxation:", [r["label"] for r in t2["relaxation"]])
    _check("S2 제거 순서 = score 낮은 한라산뷰 먼저",
           acts == [("soft_drop", "한라산뷰"), ("soft_drop", "산방산뷰")], str(acts))
    _check("S2 label 형식", t2["relaxation"][0]["label"] == "한라산뷰 조건을 빼고 찾았어요")
    _check("S2 최종 후보>0 (우도 전체)", len(t2["results"]) > 0, f"{len(t2['results'])}건")
    _check("S2 결과 전원 우도",
           all(CARDS[r["spot_name"]].get("region_bucket") == "우도" for r in t2["results"]))

    # ---- S3. 0건 -> 지역 확장 (희소 버킷 한경 + 하드 노키즈존) ----
    print("[S3] 한경 + 하드[노키즈존] -> region_expand 사다리")
    t3 = _run(_mk("한경 노키즈존 카페", "조건검색", "한경", hard=["노키즈존"]))
    print("      relaxation:", [r["label"] for r in t3["relaxation"]])
    _check("S3 확장 label", any(r["action"] == "region_expand"
                               and r["label"] == "한림, 대정까지 넓혀서 찾았어요"
                               for r in t3["relaxation"]),
           str([r["label"] for r in t3["relaxation"]]))
    _check("S3 region_expanded 기록",
           t3["region_expanded"] == {"from": "한경", "to": ["한림", "대정"]},
           str(t3["region_expanded"]))
    _check("S3 후보>0", len(t3["results"]) > 0, f"{len(t3['results'])}건")
    _check("S3 하드 보존: 결과 전원 노키즈존",
           all("노키즈존" in (CARDS[r["spot_name"]].get("tags") or []) for r in t3["results"]))
    _check("S3 확장 카드는 인접 버킷 소속",
           all(CARDS[r["spot_name"]].get("region_bucket") in ("한림", "대정") for r in t3["results"]))

    # ---- S4. 하드 불가침: 사다리 끝까지 가도 하드는 제거되지 않고 정직한 0건 ----
    print("[S4] 우도 + 하드[노키즈존,키즈친화] + 소프트[루프탑] -> 정직한 0건")
    t4 = _run(_mk("우도 노키즈존 키즈친화 루프탑", "조건검색", "우도",
                  hard=["노키즈존", "키즈친화"], soft=["루프탑"],
                  translation=[{"input": "루프탑", "tag": "루프탑", "method": "exact", "score": 1.0}]))
    print("      relaxation:", [r["label"] for r in t4["relaxation"]])
    print("      funnel:", [(s["stage"], s["n"]) for s in t4["funnel"]])
    _check("S4 정직한 0건", t4["results"] == [])
    _check("S4 intent.하드 불변", t4["intent"]["하드"] == ["노키즈존", "키즈친화"])
    _check("S4 완화는 소프트/지역만 (하드 제거 액션 없음)",
           all(r["action"] in ("soft_drop", "region_expand") for r in t4["relaxation"])
           and all(r["condition"] not in ("노키즈존", "키즈친화") for r in t4["relaxation"]))
    _check("S4 사다리 시도 흔적 (soft_drop + region_expand)",
           [r["action"] for r in t4["relaxation"]] == ["soft_drop", "region_expand"],
           str([r["action"] for r in t4["relaxation"]]))

    # ---- S5. 결정론: 같은 입력 2회 -> 완전 동일 출력 ----
    print("[S5] 결정론 (S1/S2/S4 각 2회 재실행 비교)")
    for nm, mk in [("S1", lambda: _mk("애월 오션뷰 카페", "조건검색", "애월", soft=["오션뷰"],
                                      translation=[{"input": "오션뷰", "tag": "오션뷰", "method": "exact", "score": 1.0}])),
                   ("S2", lambda: _mk("우도 산방산뷰 한라산 보이는 카페", "조건검색", "우도",
                                      soft=["산방산뷰", "한라산뷰"],
                                      translation=[{"input": "산방산뷰", "tag": "산방산뷰", "method": "exact", "score": 1.0},
                                                   {"input": "한라산 보이는", "tag": "한라산뷰", "method": "embedding", "score": 0.6}])),
                   ("S4", lambda: _mk("우도 노키즈존 키즈친화 루프탑", "조건검색", "우도",
                                      hard=["노키즈존", "키즈친화"], soft=["루프탑"],
                                      translation=[{"input": "루프탑", "tag": "루프탑", "method": "exact", "score": 1.0}]))]:
        a = _json.dumps(_run(mk()), ensure_ascii=False, sort_keys=True)
        b = _json.dumps(_run(mk()), ensure_ascii=False, sort_keys=True)
        _check(f"S5 {nm} 2회 동일", a == b)

    # ---- S6. 브라우즈: 지역만, 다수결 정렬, k 최소 12 ----
    print("[S6] 브라우즈 (애월, 조건 없음)")
    t6 = _run(_mk("애월 카페", "브라우즈", "애월"))
    bl = [r["score_parts"]["다수결"] for r in t6["results"]]
    _check("S6 결과 12건 (k 부스트)", len(t6["results"]) == 12, f"{len(t6['results'])}건")
    _check("S6 다수결 내림차순", all(a >= b for a, b in zip(bl, bl[1:])), str(bl))
    _check("S6 결과 전원 애월",
           all(CARDS[r["spot_name"]].get("region_bucket") == "애월" for r in t6["results"]))

    # ---- S7. 조회: 폐업 카드도 안내용 응답 (intent.이름 + 질의 탐지 폴백 양쪽) ----
    print("[S7] 조회 (폐업 카드 '애월빵공장')")
    t7a = _run(_mk("애월빵공장 어때", "조회", None, name="애월빵공장"))
    t7b = _run(_mk("애월빵공장 어때", "조회", None))  # 이름 미지정 -> 질의 탐지 폴백
    _check("S7 intent.이름 조회: 폐업 카드 응답",
           len(t7a["results"]) == 1 and t7a["results"][0]["closed"] is True,
           str(t7a["results"]))
    _check("S7 질의 탐지 폴백도 동일 카드",
           bool(t7b["results"]) and t7b["results"][0]["spot_name"] == t7a["results"][0]["spot_name"])
    _check("S7 조회 0건에 완화 미발동", not t7a["relaxation"])

    # ---- S8. 배제 필터: 태그 보유 카드 제거, 결과에 배제 태그 없음 ----
    print("[S8] 배제 (애월 + 배제[노키즈존] -- 아이 동반 시나리오)")
    t8 = _run(_mk("아이랑 애월 카페", "조건검색", "애월", excl=["노키즈존"]))
    exc_stage = [s for s in t8["funnel"] if s["stage"].startswith("exclude:")]
    _check("S8 exclude funnel 기록", bool(exc_stage), str(t8["funnel"]))
    _check("S8 결과에 노키즈존 없음",
           all("노키즈존" not in (CARDS[r["spot_name"]].get("tags") or []) for r in t8["results"]))
    _check("S8 후보>0", len(t8["results"]) > 0)

    # ---- 세부지역 반칸 확인 (김녕: 세부 -> 구좌 승격 경로가 라벨로 남는지) ----
    # 주의: retrieve가 세부 쿼리에도 버킷 카드를 tier 1로 이미 포함하므로 (server.py 미러)
    # 반칸이 실제 발동하려면 버킷 전체가 0건인 하드 조합이어야 한다 (모순 하드 2종 사용).
    print("[S9] 세부지역 (김녕 + 하드[노키즈존,키즈친화]) -> 반칸(구좌 승격) 라벨 확인")
    t9 = _run(_mk("김녕 노키즈존 키즈친화", "조건검색", "김녕", hard=["노키즈존", "키즈친화"]))
    print("      relaxation:", [r["label"] for r in t9["relaxation"]])
    labels9 = [r["label"] for r in t9["relaxation"]]
    _check("S9 반칸 label", "김녕 밖 구좌까지 넓혀서 찾았어요" in labels9, str(labels9))
    _check("S9 인접 확장까지 시도 후 정직한 0건",
           t9["results"] == [] and any(l.endswith("까지 넓혀서 찾았어요") and "밖" not in l
                                       for l in labels9), str(labels9))

    total = len(fails)
    if fails:
        print(f"[relax] 스모크 실패 {total}건: {fails}")
        raise SystemExit(1)
    print("[relax] 스모크 전부 통과")
