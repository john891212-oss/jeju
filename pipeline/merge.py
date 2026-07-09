# -*- coding: utf-8 -*-
"""
[파이프라인 계약] 병합 — 같은 카페의 이름 조각들을 정본 하나로 (카드 정본 생성).

배경:  spot_name(긁힌 원본 이름)이 키라서 같은 카페가 최대 9조각(프릳츠 실측).
       place_id 기준 142그룹 + place_id 없는 이름변형 26건(민옥 검수 완료 2026-07-09).
       중복은 LLM이 아니라 결정적 병합으로 푼다 — place_id는 100% 신뢰, 이름 유사도 자동 병합 금지.

입력:  data/processed/카카오플레이스.jsonl     place_id·kakao_name·주소·좌표·전화
       data/processed/중복검수_place_id.csv    X 표시 행만 병합 제외 (기본 = 전부 병합)
       data/processed/중복검수_이름변형.csv    O=병합, X=별개, 메모에 "폐업" 있으면 그룹 폐업
       data/processed/네이버 정제.jsonl        summary_blog·tags·closed_hint·richness
       data/processed/review_master.csv        판정(유지/보류/제외)·지역검색_주소
       data/raw/네이버 (재검색 )크롤링.jsonl   블로거 union의 원천 (max 금지 — 정확한 주목 수)
       data/processed/유튜브 정제.json          video_ids·mention_count·summary 폴백
       data/processed/시드라벨.json  댓글부가.json  카카오리뷰정제.jsonl  카페부가v2.json

출력:  data/processed/정본매핑.json   모든 spot_name/변형 → {canonical, place_id}
       data/processed/cards.json      카드 정본 (층 분리: 정체/요약/근거/신호)
소비자: pipeline/embed.py (재임베딩), app/server.py (검색·카드)

원칙: 좌표·주소는 네이버 지역검색 우선, 카카오 폴백 / 블로거는 union / 폐업은 지우지 않고 강등
사용:  python pipeline/merge.py
"""
import csv
import json
import os
import re
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)
RICH_ORDER = {"high": 0, "mid": 1, "low": 2}
TAG_RE = re.compile(r"<[^>]+>")

def _clean(s):
    return TAG_RE.sub("", s or "").strip()

def _norm(s):
    s = _clean(s).split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())

def load_jsonl(path):
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out

# ---- 지역 유도: 주소가 정답 (server.py에서 이관 — 카드에 확정 저장, 서버는 읽기만) ----
_EMD2BUCKET = {"애월읍": "애월", "한림읍": "한림", "한경면": "한경", "구좌읍": "구좌",
               "조천읍": "조천", "성산읍": "성산", "표선면": "표선", "남원읍": "남원",
               "안덕면": "안덕", "대정읍": "대정", "우도면": "우도", "추자면": "추자"}
_FINE_TOKENS = {"협재리": ("한림", "협재"), "곽지리": ("애월", "곽지"),
                "월정리": ("구좌", "월정리"), "세화리": ("구좌", "세화"),
                "김녕리": ("구좌", "김녕"), "종달리": ("구좌", "종달"), "송당리": ("구좌", "송당"),
                "함덕리": ("조천", "함덕"), "위미리": ("남원", "위미"),
                "사계리": ("안덕", "사계"), "중문동": ("서귀포시내", "중문"),
                "색달동": ("서귀포시내", "중문")}

def addr_to_region(a):
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


# 부속시설 오매칭 방어 (울트라마린→'울트라마린 주차장', 새빌→충전소 실측 2026-07-09):
#   주차장·충전소 = 카페가 아니라 옆 시설 — place_id 무효 (같은 카페 조각이 시설 2개에 갈라 붙기도)
#   펜션·호텔 = 겸업일 수 있어 place_id는 살리되 kakao_name(시설명)은 정본명으로 안 씀
_PID_VOID = ("주차장", "충전소", "전기차")
_NAME_VOID = ("펜션", "숙박", "호텔")

def _facility(cat, kname, spot):
    hit = lambda ws, s: any(w in (s or "") for w in ws)
    if hit(_PID_VOID, cat) or (hit(_PID_VOID, kname) and not hit(_PID_VOID, spot)):
        return "void_pid"
    if hit(_NAME_VOID, cat) or (hit(_NAME_VOID, kname) and not hit(_NAME_VOID, spot)):
        return "void_name"
    return ""

def _core(s):
    """이름 알맹이 (검수 시트와 동일 규칙) — 완전 일치만 병합 근거로 쓴다 ('테' 가짜 병합 방지)"""
    n = re.sub(r"[^\w가-힣]", "", (s or "").split("(")[0].lower())
    for g in ("카페", "커피", "베이커리", "디저트", "브런치", "제주", "제주점", "본점", "점"):
        n = n.replace(g, "")
    return n

def build_groups():
    """병합 그룹 결정 → (spot_name → 그룹키, 카카오 인덱스, 폐업 그룹 셋)"""
    kakao = {}
    void_pid_names = []   # 주차장·충전소에 매칭됐던 조각들 (pid 무효 → core 일치끼리 재병합)
    for r in load_jsonl(P("data", "processed", "카카오플레이스.jsonl")):
        n = r.get("spot_name")
        if not n:
            continue
        f = _facility(r.get("category") or "", r.get("kakao_name") or "", n)
        if f:
            r = dict(r)
            r["kakao_name"] = None            # 시설명은 정본명 후보에서 제외
            if f == "void_pid":
                r["place_id"] = None          # 오매칭 — 병합 키로도 안 씀 (좌표·주소는 폴백 유지)
                void_pid_names.append(n)
        kakao[n] = r

    # 검수① place_id 시트: X 표시 = 그 조각만 그룹에서 제외 (기본 전부 병합 — 카카오 검증 신뢰)
    pid_reject = set()
    path = P("data", "processed", "중복검수_place_id.csv")
    if os.path.exists(path):
        for r in csv.reader(open(path, encoding="utf-8-sig")):
            if len(r) >= 7 and r[6].strip().lower().startswith("x"):
                pid_reject.add((r[0].strip(), r[3].strip()))  # (place_id, spot_name)

    # place_id 그룹핑
    group_of = {}                       # spot_name → 그룹키 ("pid:..." | "name:...")
    for n, r in kakao.items():
        pid = r.get("place_id")
        if pid and (pid, n) not in pid_reject:
            group_of[n] = f"pid:{pid}"

    # pid 무효화 조각들: 알맹이 완전 일치끼리 병합 (민옥 원칙 2026-07-09 "같은 이름이면 같은 가게")
    for n in void_pid_names:
        c = _core(n)
        if len(c) >= 2:
            group_of[n] = f"name:{c}"

    # 검수② 이름변형 시트: O = 병합 (A유형: place_id 그룹에 합류 / B유형: 이름 그룹), 메모 "폐업" 파싱
    closed_groups = set()
    path = P("data", "processed", "중복검수_이름변형.csv")
    if os.path.exists(path):
        rows = list(csv.reader(open(path, encoding="utf-8-sig")))
        for r in rows[1:]:
            if len(r) < 5:
                continue
            typ, leak, pid, canon = r[0].strip(), r[1].strip(), r[2].strip(), r[3].strip()
            verdict = " ".join(c.strip() for c in r[5:])  # 검수 칸 + 자유 메모 (사람이 쓴 그대로)
            if not verdict.strip().lower().startswith("o"):
                continue  # X 또는 빈 칸 = 별개 유지
            key = f"pid:{pid}" if pid else f"name:{canon}"
            group_of[leak] = key
            if pid:  # A유형: 정본 카페 자신도 그룹에 (이미 pid 그룹이면 동일 키)
                pass
            else:    # B유형: 추정정본 이름도 같은 그룹으로
                group_of.setdefault(canon, key)
            if ("폐업" in verdict) or ("페업" in verdict):  # 민옥 메모 오타 포함
                closed_groups.add(key)
    return group_of, kakao, closed_groups


def main():
    group_of, kakao, closed_groups = build_groups()

    # ---- 전체 카페 명단 수집 (네이버 정제 ∪ 유튜브 정제 ∪ 카카오플레이스) ----
    naver = {r["spot_name"]: r for r in load_jsonl(P("data", "processed", "네이버 정제.jsonl"))
             if r.get("spot_name")}
    yt = defaultdict(list)
    for s in json.load(open(P("data", "processed", "유튜브 정제.json"), encoding="utf-8")):
        yt[s["spot_name"]].append(s)
    all_names = set(naver) | set(yt) | set(kakao)

    # 그룹 멤버 확정: 그룹 없는 이름은 자기 혼자 그룹
    members = defaultdict(set)
    for n in all_names:
        members[group_of.get(n, f"solo:{n}")].add(n)

    # ---- 판정·주소 (review_master) ----
    verdict, rm_addr = {}, {}
    path = P("data", "processed", "review_master.csv")
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            verdict[r["카페명"]] = r.get("판정", "")
            rm_addr[r["카페명"]] = r.get("지역검색_주소", "")

    # ---- 블로거 union 원천: raw 크롤링 (카페명 포함 + 2024이후 — 기존 다수결 필터 계승) ----
    bloggers_of = defaultdict(set)
    for raw_name in ("네이버 크롤링.jsonl", "네이버 재검색 크롤링.jsonl"):
        path = P("data", "raw", raw_name)
        if not os.path.exists(path):
            continue
        for rec in load_jsonl(path):
            n = rec.get("spot_name")
            if not n:
                continue
            key = _norm(rec.get("cleaned_name") or n)
            for it in rec.get("blog", {}).get("items", []):
                txt = _norm(it.get("title", "") + it.get("description", ""))
                if key and key in txt and it.get("postdate", "") >= "20240101" and it.get("bloggername"):
                    bloggers_of[n].add(it["bloggername"])

    # ---- 부가 소스 ----
    def load_json(name, default):
        path = P("data", "processed", name)
        return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else default
    seed = load_json("시드라벨.json", {})
    comm = load_json("댓글부가.json", {})
    extra = load_json("카페부가v2.json", {})
    kreview = {}
    path = P("data", "processed", "카카오리뷰정제.jsonl")
    if not os.path.exists(path):
        path = P("data", "processed", "카카오리뷰.jsonl")
    if os.path.exists(path):
        for r in load_jsonl(path):
            if r.get("spot_name"):
                kreview[r["spot_name"]] = r

    # ---- 그룹 → 카드 정본 ----
    cards, mapping = [], {}
    dup_before = sum(1 for k, ms in members.items() if len(ms) > 1 for _ in ms)
    for key, ms in members.items():
        ms = sorted(ms)
        # 정본 이름: kakao_name 우선 (place_id 그룹), 없으면 최다 블로거 조각
        pid = key[4:] if key.startswith("pid:") else None
        kk = next((kakao[n] for n in ms if kakao.get(n, {}).get("place_id") == pid), None) if pid else None
        if kk and kk.get("kakao_name"):
            name = kk["kakao_name"]
        else:
            name = max(ms, key=lambda n: (len(bloggers_of.get(n, ())), -len(n)))
        if not pid:  # 그룹에 place_id 없어도 조각 중 하나가 갖고 있으면 채택
            for n in ms:
                if kakao.get(n, {}).get("place_id"):
                    pid, kk = kakao[n]["place_id"], kakao[n]
                    break

        # 요약: 조각 중 최고 richness(동률이면 최다 블로거)의 summary_blog / 유튜브 요약은 폴백
        best_blog, best_rank = "", (9, 0)
        tags, video_ids, blog_links = set(), [], []
        mention, closed_hint = 0, False
        for n in ms:
            r = naver.get(n)
            if r:
                rank = (RICH_ORDER.get(r.get("info_richness_blog"), 9), -len(bloggers_of.get(n, ())))
                if (r.get("summary_blog") or "").strip() and rank < best_rank:
                    best_rank, best_blog = rank, r["summary_blog"]
                tags.update(r.get("tags_blog") or [])
                closed_hint = closed_hint or bool(r.get("closed_hint"))
            for s in yt.get(n, []):
                mention += 1
                if s.get("video_id") and s["video_id"] not in video_ids:
                    video_ids.append(s["video_id"])
                tags.update(s.get("tags") or [])
            e = extra.get(n) or {}
            for lk in e.get("links") or []:
                if lk not in blog_links:
                    blog_links.append(lk)
        yt_best = min((s for n in ms for s in yt.get(n, [])),
                      key=lambda s: RICH_ORDER.get(s.get("info_richness"), 9), default=None)
        summary = best_blog or (yt_best.get("summary") if yt_best else "") or ""

        # 좌표·주소: 네이버 지역검색 우선, 카카오 폴백 (민옥 결정 7/8 계승)
        lat = lng = None
        for n in ms:
            e = extra.get(n) or {}
            if e.get("lat") is not None:
                lat, lng = e["lat"], e["lng"]
                break
        if lat is None and kk and kk.get("lat") is not None:
            lat, lng = kk.get("lat"), kk.get("lng")
        address = (kk.get("road_address") or kk.get("address")) if kk else ""
        if not address:
            address = next((rm_addr[n] for n in ms if rm_addr.get(n)), "")
        rb, rf = addr_to_region(address)

        # 신호층: 시드라벨·댓글·카카오리뷰 (조각 전체에서 첫 유효값/합집합)
        caution, hours = [], ""
        tone = hint = ""
        rating_avg = rating_count = None
        review_tone = ""
        for n in ms:
            lab = seed.get(n) or {}
            tags.update(lab.get("tags_seed") or [])
            for c in lab.get("caution") or []:
                if c not in caution:
                    caution.append(c)
            hours = hours or lab.get("hours_hint") or ""
        for n in ms:
            lab = comm.get(n) or {}
            if not hours and lab.get("hours_hint"):
                hours = lab["hours_hint"] + " (댓글)"
            tone = tone or lab.get("reaction_tone", "")
            hint = hint or lab.get("reaction_hint", "")
            for v in lab.get("video_ids") or []:
                if v not in video_ids:
                    video_ids.append(v)
        for n in ms:
            r = kreview.get(n)
            if r:
                rating_avg = rating_avg if rating_avg is not None else r.get("rating_avg")
                rating_count = rating_count if rating_count is not None else r.get("rating_count")
                review_tone = review_tone or r.get("review_tone", "")
                for c in r.get("caution") or []:
                    if c not in caution:
                        caution.append(c)

        # 판정: 조각 중 '유지' 하나라도 있으면 유지 (병합으로 신호가 모인 것)
        vs = [verdict.get(n, "") for n in ms]
        v = "유지" if "유지" in vs else (next((x for x in vs if x), ""))
        closed = closed_hint or (key in closed_groups)

        bloggers = set()
        for n in ms:
            bloggers |= bloggers_of.get(n, set())

        cards.append({
            # 정체
            "name": name, "place_id": pid, "aliases": [n for n in ms if n != name],
            "address": address, "lat": lat, "lng": lng,
            "category": (kk.get("category") or "").split(">")[-1].strip() if kk else "",
            "region_bucket": rb, "region_fine": rf,
            # 요약
            "summary": summary, "tags": sorted(tags),
            # 근거
            "bloggers": len(bloggers), "mention_count": mention,
            "video_ids": video_ids[:5], "blog_links": blog_links[:3],
            # 신호
            "caution": caution, "hours_hint": hours,
            "reaction_tone": tone, "reaction_hint": hint,
            "rating_avg": rating_avg, "rating_count": rating_count, "review_tone": review_tone,
            "closed": closed, "판정": v,
        })
        for n in ms:
            mapping[n] = {"canonical": name, "place_id": pid}
        if name not in mapping:
            mapping[name] = {"canonical": name, "place_id": pid}

    cards.sort(key=lambda c: -c["bloggers"])
    json.dump(mapping, open(P("data", "processed", "정본매핑.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(cards, open(P("data", "processed", "cards.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- 리포트 ----
    merged_groups = [(k, ms) for k, ms in members.items() if len(ms) > 1]
    closed_cards = [c for c in cards if c["closed"]]
    print(f"[merge] 이름 {len(all_names)}개 → 정본 카드 {len(cards)}장 "
          f"(병합 그룹 {len(merged_groups)}개, 조각 {dup_before}개 흡수)")
    print(f"[merge] 폐업 강등 {len(closed_cards)}장: "
          + ", ".join(c["name"] for c in closed_cards[:15]) + ("…" if len(closed_cards) > 15 else ""))
    print(f"[merge] 서빙 대상(판정 유지·비폐업): "
          f"{sum(1 for c in cards if c['판정'] == '유지' and not c['closed'])}장")
    print("[merge] 블로거 union 상위 5 (병합 효과):")
    for c in cards[:5]:
        print(f"  {c['bloggers']:4d}명  {c['name']}  (조각 {len(c['aliases']) + 1}개)")

if __name__ == "__main__":
    main()
