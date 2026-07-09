# 카카오맵 리뷰 수집 — 작업 지시서 (로컬 Claude Code / Opus용)

> 설계: 코워크(SilenceBreaker) 2026-07-09. 실행: 로컬 Claude Code.
> 목적: place_id에 "믿음직하냐" 신호를 붙인다 — 카카오맵 방문자 리뷰 + 별점.
> 성격: **내부 해커톤 데이터셋**. 수집 방법(insane-search 등)은 발표에서 정직하게 공개한다.

## 0. 왜 이걸 하나 (설계 근거)

- 유튜브 쇼츠 댓글 반응은 "그 영상을 본 사람"의 반응 → 조회수 편향(내부지표 쏠림).
- 카카오 리뷰는 "실제로 가본 사람"의 반응 → **편향이 다른 두 소스가 같은 방향이면 그게 신뢰**(교차검증).
- 우리는 카카오 place_id를 영구 키로 정본화해뒀다. 지금 그 키엔 좌표·카테고리뿐 → 리뷰가 붙으면 "실존 + 여론"으로 승격.
- 가장 약한 칸이 **검증("이 카페 믿음직하냐")**. 이 작업이 정확히 그 칸을 채운다.

## 1. 스코프 — 딱 이것만

- **저장 = 리뷰 텍스트 + 별점, 둘만.** 작성자명·날짜·이미지 안 받는다 (개인정보 최소화 + 우리가 채우려는 칸엔 불필요).
- **표식 필수**: 모든 레코드에 `"source": "kakao_review_hackathon"`. 미래에 무심코 프로덕션에 끌고 가지 않게. 서비스 배포 시 이 층은 재판단.
- 절대 안 하는 것: 로그인 필요한 데이터, 작성자 신원, 리뷰 이미지 저장.

## 2. 입력 시드

- 파일: `data/processed/카카오플레이스.jsonl`
- 사용할 것: `place_id` 필드가 **존재하는 줄만** (844곳). place_id 없는 MISS는 건너뜀.
- 리뷰 페이지 URL 규칙: `https://place.map.kakao.com/{place_id}`
- 조인 키: `place_id` (불변). 출력도 place_id로 키를 맞춘다. `spot_name`도 같이 실어 눈 검수 편의.

## 3. 수집 경로 — 싼 것부터, 막히면 승격 (insane-search 철학 그대로)

카카오맵 리뷰는 JS 렌더라 정적 fetch로 HTML을 긁으면 리뷰가 안 나온다. **HTML 파싱하지 말고 내부 JSON 엔드포인트를 노려라.** 아래는 유력 가설 — 파일럿에서 실제 응답으로 검증하고, 스키마가 바뀌었으면 응답 구조를 읽어 맞춰라.

### 경로 A (1순위, 가장 쌈) — 내부 JSON API 직접 호출

카카오 플레이스는 브라우저가 뒤에서 JSON API를 때린다. 유력 후보:

```
# 장소 메인 정보 (별점 총합/개수 요약이 여기 있을 수 있음)
GET https://place.map.kakao.com/main/v/{place_id}

# 리뷰(후기) 목록 — 페이지네이션
GET https://place.map.kakao.com/commentlist/v/{place_id}/{last_comment_id}
    (첫 호출은 last_comment_id = 0)
```

**필수 헤더** (이거 없으면 403/빈 응답):
```
Referer: https://place.map.kakao.com/{place_id}
User-Agent: (일반 브라우저 UA — 모바일 UA도 시도해볼 것)
```

응답 JSON에서 찾을 것:
- 리뷰 배열 (필드명 후보: `comment.list[]`, 각 항목의 `contents`/`content` = 리뷰 텍스트, `point` = 별점)
- 별점 요약 (후보: `comment.scoresum`, `comment.scorecnt`, `comment.kamapComntcnt`)

⚠ 필드명은 확정이 아니다. 파일럿 첫 카페에서 **응답 원문을 통째로 출력해 눈으로 확인**하고 실제 키에 맞춰라. 카카오가 스키마를 바꿨을 수 있다.

### 경로 B (A가 막히면) — insane-search 승격

경로 A가 403/캡차/빈 응답으로 막히면 insane-search의 단계적 우회를 그대로 태운다:
1. curl_cffi TLS 임퍼소네이션(safari→chrome→firefox)으로 A의 JSON 엔드포인트 재시도
2. 그래도 안 되면 Playwright로 `place.map.kakao.com/{place_id}` 렌더 → "후기" 탭 클릭 → 렌더된 리뷰 DOM 추출 (**네트워크 탭에서 뜨는 XHR JSON을 가로채는 게 DOM 파싱보다 깨끗**)

## 4. 파일럿 먼저 (전량 금지)

무작정 844곳 돌리지 말 것. 7/8 유튜브 댓글 수율 실증과 같은 패턴.

1. **표본 15~20곳**: 블로거 수 상위 카페부터(리뷰가 많이 달렸을 곳 → 긁히나/안 긁히나가 빨리 판가름). 상위 목록은 `data/processed/네이버 정제.jsonl`의 고유 블로거 수 또는 `review_master.csv`로 뽑을 수 있음.
2. **측정 3개**: ① 리뷰가 실제로 긁히는가 ② 카페당 몇 개 나오는가(수율) ③ 별점이 같이 오는가.
3. **판단**: 수율 나오면 전량 확장. 클라우드 IP가 막히면 로컬 IP는 덜 막히니 로컬 실행이 유리(이건 로컬에서 도는 중이라 이미 해당).
4. 파일럿 결과(수율·샘플 3건·막힌 단계)를 짧게 보고 → 민옥·코워크가 전량 여부 결정.

## 5. 출력 스키마

- 파일: `data/processed/카카오리뷰.jsonl` (카페당 1줄, JSONL)
- **이어달리기**: 재실행 시 이미 있는 place_id는 건너뜀 (kakao_place.py의 `done` 패턴 그대로 복제)
- 카페당 1레코드:

```jsonc
{
  "place_id": "1267210144",
  "spot_name": "어드브레드",
  "source": "kakao_review_hackathon",
  "rating_avg": 4.3,        // 별점 평균 (API 요약값 있으면 그것, 없으면 리뷰들 평균)
  "rating_count": 128,      // 별점 개수 (요약값)
  "reviews": [
    {"text": "조용하고 빵이 맛있어요", "star": 5},
    {"text": "웨이팅이 좀 있음", "star": 3}
  ],
  "n_collected": 2,         // 실제 수집한 리뷰 수
  "collected_ok": true      // 수집 성공 여부 (막혔으면 false + reviews 빈 배열)
}
```

- 리뷰 상한: 카페당 **최대 20~30개**면 충분 (여론 톤 파악용이지 전수 아님). 페이지네이션 무한 추적 금지.
- 텍스트 정리: 앞뒤 공백 strip, 빈 리뷰 스킵. 별점 없는 리뷰는 `"star": null`로 보존.

## 6. 우리 파이프라인 관례 (반드시 지킬 것 — 과거에 밟은 함정)

- **ROOT 상수로 경로 고정**: `ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`. 실행 디렉토리 ≠ 스크립트 위치.
- **utf-8 / utf-8-sig**: 읽기 utf-8, 엑셀용 CSV 쓰면 utf-8-sig. 한글 깨짐 방지.
- **except가 에러를 삼키면 전멸이 0개처럼 보인다** → 실패 시 경고 출력 필수. `collected_ok: false`로 기록해 재실행 때 재시도되게(전량 스킵 금지).
- **rate limit 예의**: 요청 간 `time.sleep(0.3~1.0)`, 429/403 뜨면 지수 백오프. 카카오에 부담 주지 말 것.
- **파일 위치**: 스크립트는 `pipeline/kakao_review.py`. 출력은 `data/processed/카카오리뷰.jsonl`.
- **키 커밋 금지**: 필요 시 `.env`의 키 사용, 하드코딩·커밋 금지 (.gitignore 확인).

## 7. 손확인 (수집 후)

전량 평가는 팀원 몫. 개발 확인은 사건 카페 몇 개로:
- 리뷰 많은 유명 카페(예: 해지개, 어니스트밀크류) → 리뷰가 실제로 붙었나, 별점 그럴듯한가.
- 폐업 의심 카페 → 리뷰가 옛날에 끊겼거나 "폐업" 언급 있나 (폐업 신호 교차검증 보너스).

## 8. 소비자 (다음 단계)

이 산출물은 `merge.py` 카드 정본이 소비:
- `rating_avg`/`rating_count` → 카드 signals 층(정렬·신뢰 표시)
- `reviews` 텍스트 → evidence 층(원문 근거, /evidence가 유튜브 반응 옆에 카카오 반응도 노출)
- **임베딩 편입 금지** — 유튜브 댓글과 동일 원칙(반응 텍스트는 만능 자석 위험). 자리는 표시·정렬·근거·교차검증.

---

## 실행 순서 요약 (로컬 Claude Code에게)

1. `카카오플레이스.jsonl`에서 place_id 있는 카페 로드, 블로거 수 상위 15~20곳 표본 추출
2. 경로 A(내부 JSON API) 첫 카페로 응답 원문 확인 → 실제 필드명 파악
3. 파일럿 수집 → 수율·샘플 보고 (막혔으면 경로 B로 승격)
4. 수율 OK면 `pipeline/kakao_review.py`로 정식화(이어달리기·rate limit·에러 기록) → 전량 844곳
5. 산출물 `data/processed/카카오리뷰.jsonl` + 손확인 결과 보고
