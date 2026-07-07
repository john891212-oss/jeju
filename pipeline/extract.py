"""정제: gpt-5-mini Pass 1(추출) + Pass 2(실존검증) → data/processed/
A트랙: 설명란 있는 영상 (설명란+제목 입력)
B트랙: 설명란 없는 숏츠 (제목+태그 입력, 장소명 특정 집중)
출력: spot_name, region, category, tags, summary, info_richness
원칙: video_id는 LLM 우회(코드가 운반), 입력 1500자 절단, json 파싱 try/except, 지수 백오프"""
# TODO: 미니 관통 통과한 프롬프트 이식
