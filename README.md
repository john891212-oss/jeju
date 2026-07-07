# Jeju Trip 🍊

유튜브 숏츠/영상 기반 제주 여행 RAG 서비스.
질문을 던지면 실제 영상을 근거로 "오늘의 셋리스트(여행 코스)"를 발매합니다.

> **현재 상태 (2026-07-07)**
> - **앱(Streamlit)**: mock 데이터로 **완전히 동작** — API 키 없이 바로 실행됩니다.
> - **데이터**: 유튜브 크롤링 원본(raw) + 모델 정제본(processed)이 **git에 포함**되어 있어 `pull` 하면 바로 EDA 가능.
> - **파이프라인 스크립트**: 아직 **빈 껍데기(스텁)**. 실제 수집·정제 코드는 `notebooks/데이터탐색.ipynb`에 있습니다. (아래 "파이프라인" 참고)

## 구조

```
app/        서빙 (Streamlit) — 지금은 mock 카드로 동작, Cloud Run 배포 대상
data/
  raw/        유튜브 API 원본 json (git 포함, pull 하면 받아짐)
  processed/  모델이 정제한 스팟 자료 json + 검수용 csv (git 포함)
  mock/       앱이 실제로 읽는 샘플 카드 (cards.json)
  golden/     평가용 골든 질문셋
pipeline/   수집→정제→병합→임베딩 배치 — ⚠️ 아직 스텁, 실코드는 notebooks/
eval/       검색 품질 측정 (Hit@5)
notebooks/  데이터탐색.ipynb — 실제 수집/정제 코드가 여기 있음
```

## 빠른 시작 — 앱 띄우기 (키 불필요)

앱은 mock 데이터로 돌기 때문에 **API 키 없이 바로 실행**됩니다.

```bash
pip install -r requirements.txt
streamlit run app/main.py
```

질문을 입력하거나 추천 칩을 누르면 mock 카드로 셋리스트가 발매됩니다.
(앱이 읽는 건 `data/mock/cards.json` 하나뿐이라 다른 데이터가 없어도 동작합니다.)

## 데이터 보기 / EDA

`git pull` 하면 아래 데이터가 함께 받아집니다 (키 불필요):

| 파일 | 설명 |
|---|---|
| `data/raw/raw_20260707_cafe.json` | 유튜브 크롤링 원본 (정제 전, 카페 보강 최신본) |
| `data/raw/raw_20260707_1006.json` | 유튜브 크롤링 원본 (전 카테고리) |
| `data/processed/카페-전체자료.json` | 모델 정제본 |
| `data/processed/카페-변환.json` | 모델 정제본 (스팟 카드 형태 — EDA에 적합) |
| `data/processed/카페-csv.csv` | 검수용 표 (utf-8-sig) |

## 파이프라인 (실데이터 재생성 — 아직 미구현)

`pipeline/`의 `collect` / `extract` / `merge` / `embed` 4개 스크립트는 **현재 docstring만 있는 빈 껍데기**입니다. 그대로 실행하면 아무것도 만들어지지 않습니다. 실제로 동작하는 수집·정제 코드는 `notebooks/데이터탐색.ipynb`에 있고, 스크립트로 이식하면 아래 순서로 돌 예정입니다:

1. `.env.example` 복사 → `.env` 에 키 입력 (유튜브 `API_KEY` · OpenAI `OPENAI_KEY` · 카카오 `KAKAO_KEY`)
2. `python pipeline/collect.py` — 유튜브 수집 → `data/raw/`
3. `python pipeline/extract.py` — gpt-5-mini 정제 → `data/processed/`
4. `python pipeline/merge.py` — 동일 스팟 병합
5. `python pipeline/embed.py` — 임베딩 → `chroma_db/`

> 재실행에는 API 키 3종 + 비용(~$2)·시간(1~2h)이 들고, 유튜브 결과는 시점 의존이라 완전히 똑같이는 재현되지 않습니다. 그래서 확보한 데이터를 git에 포함해 두었습니다.

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
