# 중복 카페 검수 시트 생성 — 작업 지시서 (클로드 코드용)

> 설계: 코워크(SilenceBreaker) 2026-07-09. 실행: 클로드 코드 (병렬 작업).
> 목적: 같은 카페가 이름 여러 개로 쪼개진 걸 사람이 검수·병합할 수 있게 **한 파일로 펼친다.**
> 이건 데이터 수정의 1단계(검수 시트 생성)일 뿐. 실제 병합(merge)은 검수 결과 나온 뒤 별도.

## 배경 (왜)

`spot_name`(긁힌 원본 이름)이 카페의 키인데, 같은 카페가 이름 4~9개로 쪼개져 있다. 실측:
- **place_id 기준 중복 그룹 142개** (spot_name 2개 이상 = 같은 카페). 잉여 레코드 216개.
  예: 프릳츠 9조각(place_id 1403064814), 해일리 7조각, 런던베이글 6조각, 노을리 5조각.
- 블로거 수가 조각마다 갈라져 **주목 신호가 부패**(프릳츠 91·91·91·50·40·14·14·2·1). max만 쓰면 최대 ~9,400명분 신호 손실(상한).
- **place_id 없는 이름변형 누수 ~30개** (노을리 5번째 'Noeully Cafe' 유형) — 자동 dedup이 못 잡음.

## 핵심 원칙 (하드코딩 vs 자동 — 하이브리드)

- **place_id 있는 142그룹 = 자동 병합.** 카카오가 좌표 근접까지 검증한 번호라 100% 신뢰. 손대지 말 것.
- **place_id 없는 이름변형 = 사람 검수.** ⚠ **이름 유사도 자동 병합 금지** — 실측에서 가짜 병합 발생
  ('파스테이스'·'모살바테'·'제주다테이블'이 '테'로 잘못 묶임). 이름만 비슷한 건 다른 카페일 수 있다.
  애매하면 **안 합친다**(중복 하나가 가짜 병합보다 낫다 — kakao_place 오설록→티팩토리 함정과 동일).
- 정본 이름은 **카카오 정식명(kakao_name)** 우선.

## 입력

- `data/processed/카카오플레이스.jsonl` — spot_name, place_id, kakao_name, road_address, status
- `data/processed/네이버 정제.jsonl` — spot_name, bloggers_used (블로거 분산 표시용)
- (누수 판별 보조) `data/processed/카카오리뷰.jsonl` 등 spot_name 목록

## 출력 — 검수 시트 2개 (utf-8-sig, 엑셀용)

### 1) `data/processed/중복검수_place_id.csv` (142그룹, 자동 — 확인만)
place_id로 묶인 그룹. 한 그룹이 여러 줄(변형마다) 또는 한 줄에 변형 나열 — 엑셀에서 그룹이 눈에 보이게.
컬럼:
```
place_id, 정본후보(kakao_name), 조각수, spot_name, bloggers, road_address, 검수(비움)
```
- 그룹별로 정렬(place_id 묶음), 조각수 많은 그룹 위로.
- `검수` 칸은 비워둠 — 사람이 잘못 묶인 게 보이면 X 표시(기본은 다 병합 OK).

### 2) `data/processed/중복검수_이름변형.csv` (~30개, 손검수 필수)
place_id 없는데 기존 place_id 카페와 이름 알맹이가 겹치는 후보. **자동 병합하지 말고 O/X 받을 것.**
컬럼:
```
누수_spot_name, 추정정본_place_id, 추정정본_이름, 이름알맹이, 병합여부(비움: O=합침 X=별개)
```
- 알맹이 매칭: 일반어(카페·커피·베이커리·제주·지역명)·괄호 제거 후 `core` 비교. len(core)>=3만.
- ⚠ 후보 제시만, 판단은 사람이. 가짜 병합 후보('테'류)도 그대로 실어서 사람이 X 치게.

## 관례 (반드시)

- ROOT 상수로 경로 고정 (`os.path.dirname` 2회). 실행 위치 무관하게.
- CSV는 **utf-8-sig** (엑셀 한글 안 깨지게).
- 스크립트 위치: `pipeline/dedup_review_sheet.py`.
- 끝나면 요약 출력: 그룹 수, 잉여 레코드 수, 누수 후보 수, 블로거 분산 상위 5.

## 산출물 사용 (다음 단계 — 이 지시서 범위 아님)

검수 완료된 두 CSV → merge.py가 정본 매핑 생성:
`spot_name → {place_id, 정본이름, aliases[], 블로거 합집합(원본 크롤링 bloggername 셋 union)}`.
그 매핑 하나를 server dedup·재임베딩·카드 정본이 공통 키로 삼는다. **블로거는 max가 아니라 union**(정확한 주목 수).

## 참고: 진단 로직 (코워크가 이미 돌려 검증한 것 — 재현용)

```python
# place_id → spot_name들
pid2names = defaultdict(list)
for r in load_jsonl('카카오플레이스.jsonl'):
    if r.get('place_id') and r.get('spot_name'):
        pid2names[r['place_id']].append(r['spot_name'])
frag = [(pid, ns) for pid, ns in pid2names.items() if len(ns) > 1]   # → 142

# 이름 알맹이 정규화 (일반어 제거)
def core(s):
    n = re.sub(r'[^\w가-힣]', '', (s or '').split('(')[0].lower())
    for g in ['카페','커피','베이커리','디저트','브런치','제주','제주점','본점','점']:
        n = n.replace(g, '')
    return n
# place_id 없는 이름이 place_id 카페의 core와 겹치면 → 누수 후보(검수용, 자동병합 금지)
```
