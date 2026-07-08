# Jeju Trip 🍊

유튜브 영상/숏츠 기반 **제주 카페 RAG 서비스**.
질문을 던지면 실제 영상을 근거로 장소를 추천해주는 서비스를 제공합니다. 

> **현재 상태 (2026-07-07)**
> - **프론트(`web/`)**: 카카오맵 연동 + 검색 + 셋리스트가 구현된 **정본 프론트**. 정적 HTML+JS라 정적 서버만 있으면 바로 뜹니다.
> - **데이터**: 크롤링 원본(raw) + 모델 정제본(processed)이 **git에 포함** — `pull` 하면 바로 EDA 가능.
> - **RAG 백엔드**: 아직 **미연결**. 현재 프론트의 검색은 `web/cards.js`(정적 카드)를 클라이언트 JS로 필터링하는 mock입니다. 실 벡터검색·LLM은 다음 단계.
> - **`app/main.py` (streamlit)**: 초기 프로토타입 — **지금 정본 아님**(참고용).

## 구조

```
web/        ⭐ 정본 프론트 (HTML+JS) — 카카오맵 · 검색 · 셋리스트
  index.html       랜딩 + 지역 지도 + 검색 UI
  cards.js         카페 카드 데이터 (window.JEJU_CARDS · 지금은 정적 mock)
  config.local.js  카카오 JS 키 주입 (gitignore · 각자 로컬 생성)
app/        초기 streamlit 프로토타입 (참고용, 정본 아님)
data/
  raw/        유튜브 API 원본 json (git 포함)
  processed/  모델 정제본 json + 검수용 csv (git 포함)
  mock/       streamlit 프로토타입용 샘플 카드
  golden/     평가용 골든 질문셋
pipeline/   수집→정제→병합→임베딩 배치 — ⚠️ 아직 스텁, 실코드는 notebooks/
eval/       검색 품질 측정 (Hit@5)
notebooks/  데이터탐색.ipynb — 실제 수집/정제 코드가 여기 있음
```

## 빠른 시작 — 사이트 띄우기

`web/`은 정적 HTML+JS라 정적 서버만 있으면 됩니다.

```powershell
cd web
python -m http.server 8503
```

브라우저에서 **http://localhost:8503** 접속.

- **카카오 키가 없어도 SVG 지도로 정상 동작**합니다(개발 기본값). 검색·셋리스트·지역 지도 다 됩니다.
- 실제 카카오맵을 켜려면 아래 "카카오 실지도" 참고.

> 포트를 `8503`으로 쓰는 건 카카오 JS 키가 `localhost:8503` 도메인에 등록돼 있기 때문입니다. **SVG 폴백만 쓸 거면 아무 포트나 괜찮습니다.**

## 카카오 실지도 (선택)

카카오맵 실지도는 **등록된 JS 키 + 도메인**에서만 뜹니다. 없으면 SVG 지도로 폴백되므로 개발에는 지장이 없습니다.

실지도를 켜려면:

1. [카카오 개발자 콘솔](https://developers.kakao.com)에서 앱 생성 → **JavaScript 키** 발급
2. 그 앱의 *플랫폼 > Web > 사이트 도메인* 에 `http://localhost:8503` 등록 (+ 카카오맵 사용설정 ON)
3. `web/config.local.js` 생성 (gitignore라 커밋 안 됨):
   ```js
   window.KAKAO_JS_KEY = "발급받은_JavaScript_키";
   ```

> 현재 프로젝트의 카카오 키는 특정 노트북/도메인에만 등록돼 있어, **다른 팀원은 각자 JS 키를 발급받거나 SVG 폴백으로 개발**하면 됩니다.

## 데이터 보기 / EDA

`git pull` 하면 아래 데이터가 함께 받아집니다 (키 불필요):

| 파일 | 설명 |
|---|---|
| `data/raw/raw_20260707_cafe.json` | 유튜브 크롤링 원본 (정제 전, 카페 보강 최신본) |
| `data/raw/raw_20260707_1006.json` | 유튜브 크롤링 원본 (전 카테고리) |
| `data/processed/카페-전체자료.json` | 모델 정제본 |
| `data/processed/카페-변환.json` | 모델 정제본 (스팟 카드 형태 — EDA에 적합) |
| `data/processed/카페-csv.csv` | 검수용 표 (utf-8-sig, 엑셀용) |

## 기본 RAG / Chroma

기본 RAG DB는 `data/share/jeju_cafe_pipeline_share.json` 하나를 원천으로 만듭니다. 네이버 블로그나 명부 보강은 이후 성능 향상 레이어로 분리합니다.

체크인된 seed 문서:

| 파일 | 설명 |
|---|---|
| `data/rag/jeju_cafe_public.jsonl` | 사용자 추천용 RAG 문서 |
| `data/rag/jeju_cafe_review.jsonl` | 내부 검수용 RAG 문서 |
| `data/rag/manifest.json` | 문서 수와 출처 |

Chroma 임베딩 생성:

```powershell
python pipeline/embed_seed_rag.py
```

생성 결과:

```text
chroma_db/
```

`chroma_db/`는 로컬 생성물이라 git에 올리지 않습니다. 팀원은 seed JSONL과 `.env`의 `OPENAI_KEY` 또는 `OPENAI_API_KEY`로 동일하게 재생성할 수 있습니다.

## 파이프라인 (실데이터 재생성 — 아직 미구현)

`pipeline/`의 `collect` / `extract` / `merge` / `embed` 4개 스크립트는 **현재 docstring만 있는 빈 껍데기**입니다. 그대로 실행하면 아무것도 만들어지지 않습니다. 실제로 동작하는 수집·정제 코드는 `notebooks/데이터탐색.ipynb`에 있고, 스크립트로 이식하면 아래 순서로 돌 예정입니다:

1. `.env.example` 복사 → `.env` 에 키 입력 (유튜브 `API_KEY` · OpenAI `OPENAI_KEY` · 카카오 `KAKAO_KEY`)
2. `python pipeline/collect.py` — 유튜브 수집 → `data/raw/`
3. `python pipeline/extract.py` — gpt-5-mini 정제 → `data/processed/`
4. `python pipeline/merge.py` — 동일 스팟 병합
5. `python pipeline/embed.py` — 임베딩 → `chroma_db/`

> 재실행에는 API 키 3종 + 비용(~$2)·시간(1~2h)이 들고, 유튜브 결과는 시점 의존이라 완전히 똑같이는 재현되지 않습니다. 그래서 확보한 데이터를 git에 포함해 두었습니다.

## 실 서비스로 가는 다음 관문

현재 `web/`은 **프론트가 완성형이지만 검색이 mock**(클라이언트 JS가 `cards.js`를 필터링)입니다. 실 서비스로 키우려면:

1. **실 데이터 카드**: 파이프라인 완성 → `cards.json` 산출 → `web/cards.js`를 실데이터로 교체
2. **RAG 백엔드**: 벡터검색(Chroma) + LLM(질문분석·셋리스트 생성)을 API로 분리, 프론트가 호출
3. **배포**: 정적 프론트 + 백엔드 API (Cloud Run 등), 카카오 도메인을 배포 도메인으로 등록

## 이전 프로토타입 — streamlit

`app/main.py`는 초기 streamlit 버전(다크 네온 K-POP 컨셉)입니다. 지금 정본은 `web/` 프론트이며, streamlit은 참고용으로만 남겨둡니다.
실행(참고): `python -m streamlit run app/main.py` — mock 카드로 동작.

## 협업 규칙

- main 직접 push 금지 — 브랜치 → PR → CI 통과 → 머지
- `.env`, `config.py`, `web/config.local.js` 커밋 금지 (키 유출 주의)
- 데이터 json은 git에 포함됨 — 대량으로 새로 갱신할 땐 PR로 조율

## 파이프라인 설계 메모

- 수집: 지역×카테고리 격자 (~45 키워드) + 포토스팟 채널 보강, 무손실 raw 저장
- 정제: gpt-5-mini 2패스 (추출 → 실존검증), info_richness 3단 판정
- 텍스트 없는 숏츠도 제목/태그로 추출 (B트랙), 언급 전용 레코드로 병합
- 병합: 동일 스팟 카드 통합, mention_count = 차트인 신호
- 임베딩: text-embedding-3-large, summary만 / region은 메타데이터 필터
