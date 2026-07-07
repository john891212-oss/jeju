"""검색 [2단계]: 질문 → 관련 카드 top-k.

■ 실서비스(교체 예정):
  질문 임베딩(text-embedding-3-large) → Chroma top-k + where(region/category) 필터.
  @st.cache_resource 로 클라이언트 초기화 1회 보장.

■ 지금(뼈대):
  API 키 없이 돌도록 mock 카드에 대해
    1) 질문에서 지역/카테고리 키워드 감지 (3단계 체인의 [1] 간이판)
    2) 감지된 조건으로 필터
    3) 질문 토큰 겹침 + mention_count(차트인) 로 정렬
  으로 대체한다. 함수 시그니처는 실서비스와 동일하게 유지 → 나중에 본문만 교체.
"""
import re

from cards import load_cards

# mock 카드에 등장하는 지역 + 흔한 별칭
REGIONS = [
    "애월", "성산", "중문", "제주시내", "서귀포시내", "구좌", "한림", "조천",
    "세화", "곽지", "협재", "김녕", "함덕", "월정", "표선", "남원", "대정", "안덕",
]
REGION_ALIAS = {"서귀포": "서귀포시내", "제주시": "제주시내", "시내": "제주시내"}

# 카테고리 표현 → 카드 category 값
CATEGORY_KW = {
    "포토스팟": ["사진", "포토", "포토존", "인생샷", "뷰맛집", "예쁜"],
    "맛집": ["맛집", "식당", "점심", "저녁", "한끼", "밥집"],
    "카페": ["카페", "커피", "디저트", "베이커리", "빵", "브런치"],
}


def _detect_region(q):
    for r in REGIONS:
        if r in q:
            return r
    for alias, std in REGION_ALIAS.items():
        if alias in q:
            return std
    return None


def _detect_category(q):
    # 우선순위: 포토 > 맛집 > 카페 (카페는 거의 모든 질문에 걸려 마지막)
    for cat in ("포토스팟", "맛집", "카페"):
        if any(k in q for k in CATEGORY_KW[cat]):
            return cat
    return None


def search(query, top_k=5, region=None, category=None):
    """질문과 관련된 카드를 점수 순으로 최대 top_k개 반환.

    region/category 를 직접 넘기면 그 값을, 안 넘기면 질문에서 감지한다.
    필터가 너무 좁아 결과가 비면 단계적으로 완화한다(지역만 → 전체).
    """
    cards = load_cards()
    region = region or _detect_region(query)
    category = category or _detect_category(query)

    def match(c):
        if region and c["region"] != region:
            return False
        if category and c["category"] != category:
            return False
        return True

    pool = [c for c in cards if match(c)]
    if not pool and region:                 # 카테고리까지 걸어 비면 지역만
        pool = [c for c in cards if c["region"] == region]
    if not pool:                            # 그래도 비면 전체에서 랭킹
        pool = list(cards)

    tokens = [t for t in re.split(r"\s+", query) if len(t) >= 2]

    def score(c):
        blob = c["spot_name"] + " " + " ".join(c["tags"]) + " " + c["summary"]
        overlap = sum(1 for t in tokens if t in blob)
        return (overlap, c["mention_count"])   # 겹침 우선, 동점은 차트인 순

    pool.sort(key=score, reverse=True)
    return pool[:top_k]
