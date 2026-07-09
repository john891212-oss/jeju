# -*- coding: utf-8 -*-
"""
[파이프라인 계약] 카카오 place_id 정본화 — 이름 조인을 은퇴시키는 영구 키 발급 (트랙 2).

입력:  data/processed/review_master.csv      전 카페 (판정 포함 — 제외만 스킵)
       data/processed/카페부가v2.json         네이버 지역검색 좌표 (근접 검증용)
출력:  data/processed/카카오플레이스.jsonl     카페당 1줄 (JSONL append + 이어달리기)
       status: MATCH        이름 유사 + 좌표 500m 이내 (place_id 확정)
               MATCH_NOCOORD 이름 유사, 네이버 좌표 없어 근접 검증 불가 (사용 가능, 플래그)
               HOLD         이름 or 좌표 한쪽만 → 보류 큐 (눈 검수 — 오설록 티팩토리 유형)
               MISS         후보 없음 (실존 의심 신호 — 결정 9: 수집이 검증을 겸함)
키:    .env KAKAO_KEY (REST)
소비자: pipeline/merge.py (카드 정본의 place_id·좌표·주소·전화·카테고리)

사용:
  python pipeline/kakao_place.py          # 이어달리기 (있는 카페 스킵)
  python pipeline/kakao_place.py rebuild  # 처음부터

매칭 원칙: 일반어(카페·베이커리 등)·지역어를 걷어낸 알맹이로 비교 — 어순·병기 오염 회피.
           체인점 오매칭 방지: 좌표 어긋나면 MATCH 아니라 HOLD (실측: 오설록 티하우스→티팩토리점).
"""
import csv
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "processed", "카카오플레이스.jsonl")

env = {}
for line in open(os.path.join(ROOT, ".env"), encoding="utf-8-sig"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
KEY = env["KAKAO_KEY"]

_GENERIC = ["카페", "커피", "베이커리", "디저트", "브런치", "티하우스", "로스터리",
            "제주점", "제주", "본점", "점"]

def norm(s):
    s = (s or "").split("(")[0]
    return re.sub(r"[^\w가-힣]", "", s.lower())

def core(s):
    """일반어를 걷어낸 알맹이 — '청굴물카페' vs '카페청굴물' 어순 문제 해소"""
    n = norm(s)
    for g in _GENERIC:
        n = n.replace(norm(g), "")
    return n

def name_ok(a, b):
    ca, cb = core(a), core(b)
    if ca and cb:
        return ca == cb or ca in cb or cb in ca
    na, nb = norm(a), norm(b)
    return bool(na and nb and (na in nb or nb in na))

def dist_m(lat1, lng1, lat2, lng2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

def kakao(q, retries=3):
    url = ("https://dapi.kakao.com/v2/local/search/keyword.json?size=5&query="
           + urllib.parse.quote(q))
    req = urllib.request.Request(url, headers={"Authorization": "KakaoAK " + KEY})
    for i in range(retries):
        try:
            return json.load(urllib.request.urlopen(req, timeout=10)).get("documents", [])
        except Exception as e:
            if i == retries - 1:
                print(f"  !! API 실패 {q!r}: {type(e).__name__}: {e}")
                return None
            time.sleep(2 ** i)

def pick(name, nlat, nlng, docs):
    """후보 중 최적 1개 + status. 좌표 검증 가능하면 근접 필수."""
    best, best_rank = None, -1
    for d in docs:
        n_ok = name_ok(name, d["place_name"])
        c_ok = None
        dd = None
        if nlat and nlng:
            try:
                dd = dist_m(nlat, nlng, float(d["y"]), float(d["x"]))
                c_ok = dd < 500
            except ValueError:
                pass
        if n_ok and c_ok:
            rank = 4
        elif n_ok and c_ok is None:
            rank = 3
        elif c_ok:
            rank = 2
        elif n_ok:
            rank = 1
        else:
            rank = 0
        if rank > best_rank:
            best_rank, best = rank, (d, dd)
    if best_rank == 4:
        return "MATCH", best
    if best_rank == 3:
        return "MATCH_NOCOORD", best
    if best_rank in (1, 2):
        return "HOLD", best
    return "MISS", None

def main():
    rebuild = len(sys.argv) > 1 and sys.argv[1] == "rebuild"
    done = set()
    if not rebuild and os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                done.add(json.loads(line)["spot_name"])
            except Exception:
                pass
    elif rebuild and os.path.exists(OUT):
        os.remove(OUT)

    extra = json.load(open(os.path.join(ROOT, "data", "processed", "카페부가v2.json"), encoding="utf-8"))
    rows = list(csv.DictReader(open(os.path.join(ROOT, "data", "processed", "review_master.csv"),
                                    encoding="utf-8-sig")))
    todo = [r for r in rows if r.get("판정") != "제외" and r["카페명"] not in done]
    print(f"대상 {len(todo)} (전체 {len(rows)}, 기존 {len(done)}, 제외 스킵)")

    stats = {"MATCH": 0, "MATCH_NOCOORD": 0, "HOLD": 0, "MISS": 0, "ERR": 0}
    f = open(OUT, "a", encoding="utf-8")
    for i, r in enumerate(todo):
        name = r["카페명"]
        e = extra.get(name) or {}
        nlat, nlng = e.get("lat"), e.get("lng")
        docs = kakao(name + " 제주")
        if docs is not None and not docs:
            docs = kakao(name.split("(")[0].strip() + " 제주 카페")
        if docs is None:
            stats["ERR"] += 1
            continue  # API 실패는 기록 안 함 — 재실행 때 이어달리기로 재시도
        status, best = pick(name, nlat, nlng, docs)
        rec = {"spot_name": name, "status": status, "판정": r.get("판정")}
        if best:
            d, dd = best
            rec.update({"place_id": d["id"], "kakao_name": d["place_name"],
                        "road_address": d.get("road_address_name") or "",
                        "address": d.get("address_name") or "",
                        "lat": float(d["y"]), "lng": float(d["x"]),
                        "phone": d.get("phone") or "",
                        "category": d.get("category_name") or "",
                        "place_url": d.get("place_url") or "",
                        "dist_m": round(dd) if dd is not None else None})
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        stats[status] += 1
        if (i + 1) % 100 == 0:
            f.flush()
            print(f"  {i+1}/{len(todo)} ... {stats}")
        time.sleep(0.08)
    f.close()
    n = sum(stats.values()) or 1
    print(f"\n완료: {stats}  (MATCH율 {stats['MATCH']/n*100:.0f}%, "
          f"확보 가능 {(stats['MATCH']+stats['MATCH_NOCOORD'])/n*100:.0f}%)")

if __name__ == "__main__":
    main()
