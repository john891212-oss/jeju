# Jeju Trip 🍊

유튜브 숏츠/영상 기반 제주 여행 RAG 서비스.
질문을 던지면 실제 영상을 근거로 "오늘의 셋리스트(여행 코스)"를 발매합니다.

## 구조

```
pipeline/   인덱싱 배치 (수집 → LLM 정제 → 병합 → 임베딩) — 로컬 실행
app/        서빙 (Streamlit) — Cloud Run 배포 대상
data/
  raw/        유튜브 API 원본 (git 제외, collect.py로 재생성)
  processed/  정제된 스팟 카드 (git 제외)
  golden/     평가용 골든 질문셋 (커밋 대상!)
eval/       검색 품질 측정 (Hit@5)
notebooks/  실험실
```

## 시작하기

1. `.env.example` 복사 → `.env` 만들고 키 입력
2. `pip install -r requirements.txt`
3. 파이프라인: `python pipeline/collect.py` → `extract.py` → `merge.py` → `embed.py`
4. 앱 로컬 실행: `streamlit run app/main.py`

## 협업 규칙

- main 직접 push 금지 — 브랜치 → PR → CI 통과 → 머지
- `.env`, `config.py` 커밋 금지 (키 유출 주의)
- data/raw, processed 는 각자 로컬에서 생성

## 파이프라인 설계 메모

- 수집: 지역×카테고리 격자 (~45 키워드) + 포토스팟 채널 보강, 무손실 raw 저장
- 정제: gpt-5-mini 2패스 (추출 → 실존검증), info_richness 3단 판정
- 텍스트 없는 숏츠도 제목/태그로 추출 (B트랙), 언급 전용 레코드로 병합
- 병합: 동일 스팟 카드 통합, mention_count = 차트인 신호
- 임베딩: text-embedding-3-large, summary만 / region은 메타데이터 필터
