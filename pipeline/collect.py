"""수집: 지역×카테고리 격자 검색 → videos.list 상세 → data/raw/ 무손실 저장
쿼터: 키워드당 ~102유닛. 실행: python pipeline/collect.py"""
# TODO: 노트북에서 검증한 탐색 셀 코드 이식
# - KEYWORDS: 광역5 + 지역x카테고리 ~30 + 테마 3~4
# - publishedAfter="2023-07-01T00:00:00Z"
# - video_id 기준 dedup, keyword 필드 보존 (지역 라벨 힌트)
