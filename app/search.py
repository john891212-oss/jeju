"""검색: 질문 임베딩(text-embedding-3-large) → Chroma top-k
- 질문에서 지역명 감지 시 where 필터 (region 정확 매칭)
- @st.cache_resource 로 클라이언트 초기화 1회 보장"""
