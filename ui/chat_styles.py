# ui/chat_styles.py
# ============================================================
# 채팅 화면 스타일(CSS)과 말풍선 렌더링 담당.
# 🌟 [모듈화] ui/chat_app.py가 너무 길어져서 관리 위험이 커진다는 판단 하에,
# 비즈니스 로직이 전혀 없는 순수 화면 꾸미기 부분(CSS 주입, 말풍선 렌더링, 타이핑
# 애니메이션)만 먼저 이 파일로 분리했다. chat_app.py는 이 파일의 inject_chat_css(),
# render_message()만 가져다 쓴다.
# ============================================================
import html as html_lib
import time
import streamlit as st


# 🌟 [UI 리디자인] 브랜드 컬러(#2563EB, .streamlit/config.toml의 primaryColor와 동일)를
# 기준으로 사이드바/채팅창/버튼/카드류를 하나의 톤으로 다듬었다. 전부 스트림릿의
# 공식 data-testid 셀렉터(버전에 안전) 또는 이 파일 안에서만 쓰는 hp- 접두사 클래스만
# 사용하고, 스트림릿 내부 구조나 기존 로직은 전혀 건드리지 않는다(순수 시각 변경).
_CHAT_CSS = """
<style>
@keyframes hp-fade-in {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
}

/* ---------- 전체 레이아웃: 채팅은 너무 넓으면 읽기 힘들어 가운데로 정렬 ---------- */
[data-testid="stMainBlockContainer"] {
    max-width: 820px;
    padding-top: 2rem;
    padding-bottom: 6rem;
}

/* ---------- 상단 타이틀 영역 ---------- */
[data-testid="stMainBlockContainer"] h1 {
    font-size: 1.6rem;
    font-weight: 800;
    letter-spacing: -0.01em;
    margin-bottom: 0.1rem;
}

/* ---------- 사이드바 ---------- */
[data-testid="stSidebar"] {
    border-right: 1px solid #E3E8F2;
}
[data-testid="stSidebar"] h1 {
    font-size: 1.15rem;
    font-weight: 800;
    padding: 0.2rem 0 0.6rem 0;
}
[data-testid="stSidebar"] hr {
    margin: 0.9rem 0;
    border-color: #E3E8F2;
}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.72rem;
    font-weight: 700;
    color: #8A93A6;
}
/* 대화 목록/새 대화 버튼: nav 아이템처럼 좌측정렬 + 은은한 hover */
[data-testid="stSidebar"] [data-testid="stButton"] button {
    border-radius: 10px;
    text-align: left;
    justify-content: flex-start;
    font-weight: 500;
    transition: background-color 0.15s ease, transform 0.1s ease;
}
[data-testid="stSidebar"] [data-testid="stButton"] button:hover {
    background-color: #EEF2FF;
    border-color: #C7D6FB;
}
[data-testid="stSidebar"] [data-testid="stButton"] button p {
    text-overflow: ellipsis;
    overflow: hidden;
    white-space: nowrap;
}
[data-testid="stSidebar"] [data-testid="stExpander"] {
    border-radius: 12px;
    border-color: #E3E8F2;
}

/* ---------- 대화 액션 카드(안내문/조건 요약) ---------- */
[data-testid="stAlert"] {
    border-radius: 14px;
    border: 1px solid #DCE6FE;
    box-shadow: 0 1px 2px rgba(37, 99, 235, 0.05);
}

/* ---------- 확정 결과 카드 ---------- */
[data-testid="stMetric"] {
    background: linear-gradient(135deg, #F0F5FF 0%, #F7F9FF 100%);
    border-radius: 14px;
    padding: 0.9rem 1.1rem;
    border: 1px solid #E3E8F2;
}
[data-testid="stMetricValue"] {
    color: #1D4ED8;
    font-weight: 800;
}
[data-testid="stExpander"] {
    border-radius: 14px;
    border-color: #E3E8F2;
}

/* ---------- 다운로드 버튼: 확인/카피 버튼(파랑)과 구분되는 보조 액션 톤 ---------- */
[data-testid="stDownloadButton"] button {
    border-radius: 10px;
    border-color: #B7E4D8;
    background-color: #F0FBF7;
    color: #0E7A5F;
    font-weight: 600;
    transition: background-color 0.15s ease;
}
[data-testid="stDownloadButton"] button:hover {
    background-color: #DFF6EC;
    border-color: #0E7A5F;
    color: #0E7A5F;
}

/* ---------- 일반 버튼 다듬기 ---------- */
[data-testid="stButton"] button {
    transition: transform 0.05s ease, box-shadow 0.15s ease;
}
[data-testid="stButton"] button:active {
    transform: scale(0.98);
}
[data-testid="stBaseButton-primary"] {
    box-shadow: 0 2px 10px rgba(37, 99, 235, 0.25);
}

/* ---------- 채팅 입력창 ---------- */
[data-testid="stChatInput"] {
    border-radius: 16px;
    box-shadow: 0 2px 12px rgba(21, 33, 66, 0.08);
}

/* ---------- 채팅 말풍선 ---------- */
.hp-chat-row { display: flex; margin: 10px 0; animation: hp-fade-in 0.25s ease; }
.hp-chat-row.hp-user { justify-content: flex-end; }
.hp-chat-row.hp-assistant { justify-content: flex-start; }
.hp-chat-bubble {
    max-width: 70%;
    padding: 11px 16px;
    border-radius: 18px;
    white-space: pre-wrap;
    word-wrap: break-word;
    line-height: 1.55;
    font-size: 0.95rem;
}
.hp-chat-bubble.hp-user {
    background: linear-gradient(135deg, #2563EB 0%, #1D4ED8 100%);
    color: #ffffff;
    border-bottom-right-radius: 4px;
    box-shadow: 0 3px 10px rgba(37, 99, 235, 0.25);
}
.hp-chat-bubble.hp-assistant {
    background: #F4F6FB;
    color: #1B2333;
    border: 1px solid #E3E8F2;
    border-bottom-left-radius: 4px;
}
</style>
"""


def inject_chat_css():
    # 세션당 한 번만 넣어도 되지만, st.markdown은 중복 삽입되어도 부작용이 없어
    # 매 렌더링마다 넣어도 안전하다 (핀포인트: 스타일 정의만 하는 블록).
    st.markdown(_CHAT_CSS, unsafe_allow_html=True)


def _bubble_html(role, text):
    css_role = "hp-user" if role == "user" else "hp-assistant"
    safe_text = html_lib.escape(text).replace("\n", "<br>")
    return f'<div class="hp-chat-row {css_role}"><div class="hp-chat-bubble {css_role}">{safe_text}</div></div>'


def render_message(role, text, animate=False, delay=0.02, chunk_size=2):
    """
    말풍선 하나를 그린다. animate=True면 챗지피티/제미나이처럼 글자가
    타이핑되는 효과를 낸다 (방금 생성된 마지막 메시지에만 사용).

    🌟 [UX 개선] 기존에는 메시지가 길면 덩어리(chunk) 단위로 한 번에 왕창 렌더링하도록
    강제 최적화가 되어 있어 어색했습니다. 이 로직을 지우고, 고정된 글자 수(2글자)와
    속도(0.02초)로 출력되게 수정하여 실제 AI가 실시간으로 답변을 스트리밍하는 것과
    똑같은 시각적 효과를 줍니다.
    """
    if not animate:
        st.markdown(_bubble_html(role, text), unsafe_allow_html=True)
        return

    placeholder = st.empty()
    accumulated = ""

    # 글자 길이에 상관없이 무조건 2글자씩 일정한 속도로 타다닥 타이핑합니다.
    for i in range(0, len(text), chunk_size):
        accumulated += text[i:i + chunk_size]
        placeholder.markdown(_bubble_html(role, accumulated), unsafe_allow_html=True)
        time.sleep(delay)
