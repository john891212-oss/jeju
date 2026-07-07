"""카드 데이터 로더.

지금은 mock JSON(`data/mock/cards.json`)을 읽는다. 트랙 A(데이터 파이프라인)가
`chroma_db/` + `data/processed/cards.json` 를 산출하면, 이 파일의 `load_cards()`
본문만 그쪽 로더로 교체하면 된다. 다른 코드(search/generate/main)는 손대지 않는다.

경로는 실행 디렉토리와 무관하도록 ROOT 상수로 고정 (HANDOFF 함정 재발 방지).
"""
from pathlib import Path
import json
import functools

ROOT = Path(__file__).resolve().parent.parent          # 프로젝트 루트
MOCK_CARDS = ROOT / "data" / "mock" / "cards.json"


@functools.lru_cache(maxsize=1)
def load_cards():
    """스팟 카드 리스트를 반환. 스키마는 project_status.md §5 계약을 따른다.

    나중에 이 본문을 chroma_db 컬렉션 로드로 바꾸면 실데이터로 전환된다.
    """
    with open(MOCK_CARDS, encoding="utf-8") as f:
        return json.load(f)
