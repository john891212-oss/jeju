"""셋리스트 생성 [3단계]: 검색된 카드 → "오늘의 셋리스트(코스)".

■ 실서비스(교체 예정):
  gpt-5-mini 로 카드만 근거로 코스(트랙 순서/시간대/한줄 카피)를 구성.
  **카드에 없는 내용 생성 금지**(근거 충실성). 톤: K-POP 발매 문법.

■ 지금(뼈대):
  LLM 없이 규칙으로 트랙을 배치한다. 카피는 카드의 summary/tags 에서만 뽑아
  환각을 원천 차단한다(규칙 기반이라 애초에 지어낼 수 없음). 반환 구조는
  실서비스와 동일 → 나중에 make_setlist 본문만 LLM 호출로 교체.
"""
import re

# 앨범 트랙 은유 (여행 코스의 시간대 흐름을 K-POP 발매 문법으로)
SLOT_LABELS = ["🎬 오프닝", "⭐ 타이틀곡", "🎧 수록곡", "🎧 수록곡", "🔥 앙코르"]


def _first_sentence(summary):
    """summary 의 첫 문장만 (카피용). 카드에 있는 문장만 쓴다 = 환각 없음."""
    if not summary:
        return ""
    parts = re.split(r"(?<=[.!?。])\s|(?<=[다요])\.", summary.strip())
    s = parts[0].strip() if parts else summary.strip()
    return (s[:70] + "…") if len(s) > 71 else s


def _album_title(query, cards):
    if cards:
        region = cards[0]["region"]
        return f"제주 {region} EP — '{query.strip()}'"
    return f"'{query.strip()}' 셋리스트"


def make_setlist(query, cards):
    """카드 리스트를 트랙 코스로 편성해 반환.

    반환 스키마:
      { query, title, intro, tracks: [ {track_no, slot, card, why} ] }
    """
    tracks = []
    for i, c in enumerate(cards):
        tracks.append({
            "track_no": i + 1,
            "slot": SLOT_LABELS[i] if i < len(SLOT_LABELS) else "🎧 수록곡",
            "card": c,
            "why": _first_sentence(c.get("summary", "")),   # 카드 근거 한 줄
        })

    if tracks:
        intro = f"'{query.strip()}' 셋리스트가 도착했어요. 총 {len(tracks)}트랙 🍊"
    else:
        intro = "조건에 맞는 트랙을 찾지 못했어요. 다른 키워드로 검색해 보세요."

    return {
        "query": query,
        "title": _album_title(query, cards),
        "intro": intro,
        "tracks": tracks,
    }
