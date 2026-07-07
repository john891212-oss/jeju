"""Streamlit 엔트리 — 질문 입력 → "오늘의 셋리스트" 발매 화면.

디자인 기준: jeju_pop_trail_design.html (다크 스테이지, 핑크/시안, 트랙 카드).
데이터: 지금은 mock 카드(app/cards.py). 실데이터 전환 시 백엔드 함수 본문만 교체됨.
실행: streamlit run app/main.py   (API 키 없이 mock 으로 전 과정 동작)
"""
import html
import os
import sys

import streamlit as st

# 실행 방식(streamlit run / Docker / AppTest / VS Code)과 무관하게 app/ 을 import 경로에 고정.
# HANDOFF 함정: "실행 디렉토리 ≠ 스크립트 위치" — 스크립트가 스스로 자기 위치를 경로에 넣는다.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from search import search        # noqa: E402  (경로 고정 후 import 해야 함)
from generate import make_setlist  # noqa: E402

st.set_page_config(page_title="Jeju POP Trail", page_icon="🍊", layout="centered")

# ── 스타일 (다크 스테이지 + 네온 핑크/시안) ────────────────────────────────
st.markdown(
    """
    <style>
      .stApp { background: radial-gradient(1200px 600px at 50% -10%, #241a33 0%, #0e0b16 55%); }
      .hero-title { font-size: 2.6rem; font-weight: 800; letter-spacing: -1px; margin: 0;
                    background: linear-gradient(90deg,#ff4d94 0%,#35e0d8 100%);
                    -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
      .hero-sub { color: #b9b3c9; margin-top: .2rem; font-size: .98rem; }
      .album-line { color:#35e0d8; font-weight:700; letter-spacing:2px; font-size:.8rem; }

      .track-card { display:flex; gap:14px; background:#171223; border:1px solid #2c2440;
                    border-radius:16px; padding:12px; margin:12px 0;
                    box-shadow:0 6px 24px rgba(0,0,0,.35); }
      .thumb { width:150px; min-width:150px; height:100px; border-radius:12px; overflow:hidden;
               background:linear-gradient(135deg,#ff4d94,#35e0d8); position:relative; }
      .thumb img { width:100%; height:100%; object-fit:cover; display:block; }
      .slot-badge { position:absolute; top:6px; left:6px; background:rgba(14,11,22,.82);
                    color:#fff; font-size:.72rem; font-weight:700; padding:2px 8px; border-radius:999px; }
      .track-body { flex:1; min-width:0; }
      .track-name { font-size:1.12rem; font-weight:800; color:#fff; }
      .chart-pill { color:#0e0b16; background:#35e0d8; font-size:.72rem; font-weight:800;
                    padding:2px 8px; border-radius:999px; margin-left:8px; vertical-align:middle; }
      .track-region { color:#ff9ec4; font-size:.82rem; margin:2px 0 4px; font-weight:600; }
      .track-tags { color:#8fe9e2; font-size:.8rem; margin-bottom:5px; }
      .track-why { color:#cfc8de; font-size:.9rem; line-height:1.45; }
      .track-link a { color:#ff4d94; font-size:.82rem; font-weight:700; text-decoration:none; }
      .setlist-intro { color:#f0eef5; font-size:1.05rem; font-weight:700; margin:6px 0 2px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── 헤더 ────────────────────────────────────────────────────────────────
st.markdown('<div class="album-line">NEW RELEASE · JEJU CAFE</div>', unsafe_allow_html=True)
st.markdown('<div class="hero-title">Jeju POP Trail 🍊</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="hero-sub">유튜브 영상이 고른 제주 카페. 질문하면 '
    '<b>오늘의 셋리스트(코스)</b>를 발매해 드려요.</div>',
    unsafe_allow_html=True,
)
st.write("")

# ── 추천 칩 (질문 입력창보다 먼저 두어야 세션 상태 주입이 안전) ─────────────
DEMO_CHIPS = ["애월 오션뷰 카페", "성산 사진 예쁜 곳", "제주시내 베이커리", "노을 지는 감성 카페"]
st.caption("이런 걸 물어보세요")
chip_cols = st.columns(len(DEMO_CHIPS))
for i, chip in enumerate(DEMO_CHIPS):
    if chip_cols[i].button(chip, key=f"chip_{i}", use_container_width=True):
        st.session_state["q_input"] = chip

query = st.text_input(
    "질문",
    key="q_input",
    placeholder="예: 비 오는 날 애월에서 갈 만한 카페",
    label_visibility="collapsed",
)


# ── 트랙 카드 렌더 ───────────────────────────────────────────────────────
def render_track(track):
    c = track["card"]
    vid = c["video_ids"][0] if c.get("video_ids") else ""
    thumb = f"https://img.youtube.com/vi/{vid}/mqdefault.jpg" if vid else ""
    link = f"https://youtube.com/watch?v={vid}" if vid else ""
    tags = " ".join(f"#{html.escape(t)}" for t in c.get("tags", [])[:4])
    img_html = f'<img src="{thumb}" alt="">' if thumb else ""
    link_html = (
        f'<div class="track-link"><a href="{link}" target="_blank">▶ 근거 영상 보기</a></div>'
        if link else ""
    )
    st.markdown(
        f"""
        <div class="track-card">
          <div class="thumb">{img_html}<span class="slot-badge">{html.escape(track["slot"])}</span></div>
          <div class="track-body">
            <div class="track-name">{html.escape(c["spot_name"])}
              <span class="chart-pill">차트인 {c["mention_count"]}회</span></div>
            <div class="track-region">📍 {html.escape(c["region"])} · {html.escape(c["category"])}</div>
            <div class="track-tags">{tags}</div>
            <div class="track-why">{html.escape(track["why"])}</div>
            {link_html}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── 검색 → 셋리스트 ─────────────────────────────────────────────────────
if query and query.strip():
    cards = search(query, top_k=5)
    setlist = make_setlist(query, cards)
    st.markdown(f'<div class="setlist-intro">{html.escape(setlist["intro"])}</div>', unsafe_allow_html=True)
    st.caption(setlist["title"])
    if not setlist["tracks"]:
        st.info("조건에 맞는 트랙이 없어요. 지역이나 키워드를 바꿔 보세요.")
    for track in setlist["tracks"]:
        render_track(track)
else:
    st.caption("위 칩을 누르거나 질문을 입력하면 셋리스트가 발매됩니다.")
