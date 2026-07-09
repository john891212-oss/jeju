# -*- coding: utf-8 -*-
"""
[파이프라인 계약] 카카오맵 방문자 후기 + 별점 수집 — place_id에 "믿음직하냐" 신호 부착.

성격:  내부 해커톤 데이터셋. 수집 방법은 발표에서 정직하게 공개.
       모든 레코드에 source="kakao_review_hackathon" (프로덕션 무단 반입 방지 — 배포 시 재판단).

입력:  data/processed/카카오플레이스.jsonl   place_id 있는 줄만 (844곳). MISS는 스킵.
출력:  data/processed/카카오리뷰.jsonl        카페당 1줄 (JSONL append + 이어달리기)

엔드포인트 (2026-07-09 실측 — 브라우저 XHR 캡처로 발견, 카카오가 구 스키마 폐기):
  GET https://place-api.map.kakao.com/places/tab/reviews/kakaomap/{place_id}?page=N
  필수 헤더: appVersion:6.6.0, pf:PC  (없으면 406. 키 아님 — 공개 헤더값)
  응답: score_set{average_score,review_count} + reviews[]{star_rating,contents,...} + has_next
  ※ blog 후기(/reviews/blog/)는 별점 없고 네이버 블로그와 겹쳐 미수집. kakaomap만 = 방문자+별점.

스코프: 리뷰 텍스트 + 별점만 저장. 작성자·날짜·이미지 미수집 (개인정보 최소화 + 불필요).
상한:  카페당 최대 30개 (여론 톤 파악용, 전수 아님). 페이지네이션 무한 추적 금지.

소비자: pipeline/merge.py — rating_avg/count → 카드 signals층, reviews → evidence층.
        임베딩 편입 금지 (유튜브 댓글과 동일: 반응 텍스트는 만능 자석 위험).

사용:
  python pipeline/kakao_review.py          # 이어달리기 (성공분 스킵, 실패분 재시도)
  python pipeline/kakao_review.py rebuild  # 처음부터
"""
import json
import os
import sys
import time

from curl_cffi import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACE = os.path.join(ROOT, "data", "processed", "카카오플레이스.jsonl")
OUT = os.path.join(ROOT, "data", "processed", "카카오리뷰.jsonl")

SOURCE = "kakao_review_hackathon"
MAX_REVIEWS = 30
PAGE_LIMIT = 10  # 안전상한 (has_next 무한 방지)
URL_T = "https://place-api.map.kakao.com/places/tab/reviews/kakaomap/{}?page={}"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "appVersion": "6.6.0",  # ← 없으면 406 (카카오 place-api 필수 헤더)
    "pf": "PC",
    "Referer": "https://place.map.kakao.com/",
    "Origin": "https://place.map.kakao.com",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
}


def get_page(pid, page, retries=4):
    """1페이지 요청. 429/403은 지수 백오프 재시도. (json|None, note)"""
    for i in range(retries):
        try:
            r = requests.get(URL_T.format(pid, page), headers=HEADERS,
                             impersonate="chrome", timeout=15)
        except Exception as e:
            if i == retries - 1:
                return None, f"{type(e).__name__}: {e}"
            time.sleep(2 ** i)
            continue
        if r.status_code == 200:
            return r.json(), "ok"
        if r.status_code in (429, 403):
            time.sleep(2 ** i + 1)  # 예의: 백오프
            continue
        return None, f"HTTP {r.status_code}"
    return None, "retry_exhausted"


def collect(pid):
    """카카오 방문자 후기 수집. (rating_avg, rating_count, reviews, ok, note)"""
    reviews, score_set = [], None
    page = 1
    while page <= PAGE_LIMIT:
        j, note = get_page(pid, page)
        if j is None:
            # 첫 페이지부터 실패면 수집 실패. 2페이지+ 실패면 부분 성공으로 마감.
            if page == 1:
                return None, None, [], False, note
            break
        if score_set is None:
            score_set = j.get("score_set") or {}
        for rv in (j.get("reviews") or []):
            txt = (rv.get("contents") or "").strip()
            if not txt:
                continue  # 빈 리뷰 스킵
            star = rv.get("star_rating")
            reviews.append({"text": txt, "star": star if star is not None else None})
            if len(reviews) >= MAX_REVIEWS:
                break
        if len(reviews) >= MAX_REVIEWS or not j.get("has_next"):
            break
        page += 1
        time.sleep(0.3)
    avg = (score_set or {}).get("average_score")
    cnt = (score_set or {}).get("review_count")
    return avg, cnt, reviews, True, "done"


def load_done(rebuild):
    """이어달리기: 성공(collected_ok=true)한 place_id만 스킵. 실패분은 재시도."""
    done = set()
    if rebuild and os.path.exists(OUT):
        os.remove(OUT)
        return done
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                d = json.loads(line)
                if d.get("collected_ok"):
                    done.add(d["place_id"])
            except Exception:
                pass
    return done


def main():
    rebuild = len(sys.argv) > 1 and sys.argv[1] == "rebuild"
    done = load_done(rebuild)

    # place_id 있는 카페만
    todo = []
    for line in open(PLACE, encoding="utf-8"):
        d = json.loads(line)
        pid = d.get("place_id")
        if pid and pid not in done:
            todo.append((pid, d["spot_name"]))
    print(f"대상 {len(todo)}곳 (기존 성공 {len(done)}곳 스킵)")

    stats = {"ok": 0, "empty": 0, "fail": 0}
    tot_reviews = 0
    f = open(OUT, "a", encoding="utf-8")
    for i, (pid, name) in enumerate(todo, 1):
        try:
            avg, cnt, reviews, ok, note = collect(pid)
        except Exception as e:
            print(f"  !! {name} ({pid}): {type(e).__name__}: {e}")  # 삼키지 말 것
            ok, avg, cnt, reviews, note = False, None, None, [], str(e)
        rec = {
            "place_id": pid,
            "spot_name": name,
            "source": SOURCE,
            "rating_avg": avg,
            "rating_count": cnt,
            "reviews": reviews,
            "n_collected": len(reviews),
            "collected_ok": ok,
        }
        if not ok:
            rec["error"] = note
            stats["fail"] += 1
        elif reviews:
            stats["ok"] += 1
        else:
            stats["empty"] += 1  # 카카오 후기 0개 (정직 기록)
        tot_reviews += len(reviews)
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if i % 50 == 0:
            f.flush()
            print(f"  {i}/{len(todo)} ... 성공{stats['ok']} 빈곳{stats['empty']} "
                  f"실패{stats['fail']} 리뷰{tot_reviews}개")
        time.sleep(0.4)  # 예의
    f.close()
    print(f"\n완료: 성공 {stats['ok']} / 빈곳 {stats['empty']} / 실패 {stats['fail']} "
          f"| 총 리뷰 {tot_reviews}개")
    if stats["fail"]:
        print(f"  (실패 {stats['fail']}곳은 재실행 시 자동 재시도)")


if __name__ == "__main__":
    main()
