# ui/chat_app.py
import io
import json
import time
import html as html_lib
import streamlit as st
from ai_engine.gemini_api import (
    generate_target_chat_reply, generate_target_reasoning, generate_ai_push_copy,
)
from database.db_manager import (
    apply_target_conditions, format_target_summary, format_conditions_line, summarize_profile_context,
    summarize_segment_insight, format_segment_insight_reply, save_conversation, make_conversation_title,
)
from utils.response_parser import parse_target_conditions, format_push_copy_for_display
from utils.naver_search import search_term_meaning


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


def _inject_chat_css():
    # 세션당 한 번만 넣어도 되지만, st.markdown은 중복 삽입되어도 부작용이 없어
    # 매 렌더링마다 넣어도 안전하다 (핀포인트: 스타일 정의만 하는 블록).
    st.markdown(_CHAT_CSS, unsafe_allow_html=True)


def _bubble_html(role, text):
    css_role = "hp-user" if role == "user" else "hp-assistant"
    safe_text = html_lib.escape(text).replace("\n", "<br>")
    return f'<div class="hp-chat-row {css_role}"><div class="hp-chat-bubble {css_role}">{safe_text}</div></div>'


def _render_message(role, text, animate=False, delay=0.02, chunk_size=2):
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


def _build_customer_id_excel(df):
    """캠페인 업로드 양식(R-고객번호 단일 컬럼)에 맞춘 엑셀 바이트 생성."""
    buffer = io.BytesIO()
    export_df = df[['R고객번호']].astype(str).rename(columns={'R고객번호': 'R-고객번호'})
    export_df.to_excel(buffer, index=False, sheet_name='이웃고객관리목록')
    return buffer.getvalue()


def process_target_turn(messages, conditions, user_text, profile_df, db_audience=None):
    """타겟 설정 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능)."""
    history_with_user = messages + [{"role": "user", "text": user_text}]
    profile_context_str = summarize_profile_context(profile_df)
    conditions_str = json.dumps(conditions or {}, ensure_ascii=False)

    ai_raw = generate_target_chat_reply(history_with_user, profile_context_str, conditions_str)
    reply_text, parsed_conditions = parse_target_conditions(ai_raw)
    parsed_conditions = parsed_conditions or {}

    # 🌟 [신조어/속어 이해 고도화] "꽃중년"처럼 AI가 뜻을 확신하지 못하는 표현은 추측하지
    # 않고 "확인필요단어"에만 담아 돌려주도록 프롬프트에서 유도했다. 이런 단어가 있으면
    # (설정되어 있다면) 검색으로 실제 뜻을 확인한 뒤, 그 뜻을 프롬프트에 더해 같은 턴을
    # 한 번 더 해석시킨다. 🌟 [2026-09 기준] 검색 연동 자체(utils/naver_search.py)는
    # 당분간 보류하기로 해서 API 키가 없는 상태 - 이 경우 search_term_meaning()이 항상
    # None을 반환하므로 아래 if는 통과되지 않고, 1차 응답에서 AI가 직접 실무자에게
    # 되물은 질문이 그대로 답변으로 나간다(무리하게 추측하는 것보다 안전). 추후 검색
    # 연동을 켜면 코드 변경 없이 자동으로 이 2차 재해석 경로가 활성화된다.
    unclear_terms = parsed_conditions.pop('확인필요단어', None) or []
    if unclear_terms:
        term_defs = []
        for term in unclear_terms[:3]:  # 한 턴에 걸리는 검색 호출 수 상한
            snippet = search_term_meaning(term)
            if snippet:
                term_defs.append(f"[{term}]\n{snippet}")
        if term_defs:
            term_context_str = "\n\n".join(term_defs)
            ai_raw2 = generate_target_chat_reply(
                history_with_user, profile_context_str, conditions_str, term_context_str,
            )
            reply_text2, parsed_conditions2 = parse_target_conditions(ai_raw2)
            if parsed_conditions2:
                parsed_conditions2.pop('확인필요단어', None)
                reply_text, parsed_conditions = reply_text2, parsed_conditions2

    # 🌟 [탐색형 대화 고도화] "질문조건"은 확정 타겟이 아니라, "이 세그먼트는 뭘 가장
    # 많이 봐?"처럼 실무자가 순수하게 궁금해서 물어본 세그먼트를 계산하기 위한 1회성
    # 값이다. 아래 조건 병합 루프에 절대 섞이면 안 되므로(섞이면 질문 한 번에 확정
    # 타겟이 의도치 않게 바뀐다) 먼저 따로 떼어낸다.
    question_conditions = parsed_conditions.pop('질문조건', None)

    merged_conditions = dict(conditions or {})
    for k, v in (parsed_conditions or {}).items():
        # 🌟 [타겟팅 고도화] 제외조건은 dict 값이라 "v not in (None, [], '')" 비교로는
        # 빈 dict({})를 걸러낼 수 없다(빈 dict는 저 셋 중 무엇과도 == 이 아니므로 그대로
        # 통과되어, AI가 매턴 빈 제외조건을 같이 내려줄 때마다 이전에 실무자가 확정해둔
        # 제외조건을 실수로 지워버리는 문제가 생긴다). dict는 "비어있지 않을 때만" 병합.
        if isinstance(v, dict):
            if v:
                merged_conditions[k] = v
        elif v not in (None, [], ""):
            merged_conditions[k] = v

    # 🌟 [탐색형 대화 고도화] 질문으로 판단된 턴이면, AI가 답을 지어내는 대신 시스템이
    # 실제 데이터를 계산(summarize_segment_insight)한 뒤 그 결과만 근거로 답변을 다시
    # 만든다. 확정 조건(merged_conditions)에는 전혀 반영하지 않으므로, 실무자가 "그
    # 조건으로 타겟 잡아줘"라고 명시적으로 말해야만 다음 턴에 실제 타겟 조건으로 넘어간다.
    # 🌟 [오류/속도 고도화] 이 답변은 계산된 숫자를 문장으로 옮기기만 하면 되므로,
    # 여기서 추가로 제미나이를 한 번 더 호출하지 않고 format_segment_insight_reply()로
    # 즉시 문장을 만든다 - 대화 한 턴당 API 호출이 1회 줄어 429(요청 한도 초과) 위험과
    # 응답 지연이 함께 줄어든다.
    if isinstance(question_conditions, dict) and question_conditions:
        insight = summarize_segment_insight(profile_df, question_conditions, db_audience)
        reply_text = format_segment_insight_reply(insight)

    new_messages = history_with_user + [{"role": "assistant", "text": reply_text}]
    return new_messages, merged_conditions


def process_copy_turn(messages, target_summary_str, reasoning, user_text):
    """카피 작성 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능)."""
    raw = generate_ai_push_copy(target_summary_str, reasoning, user_text or "")
    copy_text = format_push_copy_for_display(raw)
    new_messages = messages + ([{"role": "user", "text": user_text}] if user_text else [])
    new_messages = new_messages + [{"role": "assistant", "text": copy_text}]
    return new_messages, copy_text


def _autosave():
    member_ids = (
        st.session_state.target_result_df['R고객번호'].astype(str).tolist()
        if st.session_state.target_result_df is not None else []
    )
    first_user_text = next((m['text'] for m in st.session_state.messages if m['role'] == 'user'), "")
    save_conversation(
        conv_id=st.session_state.current_conversation_id,
        title=make_conversation_title(first_user_text),
        phase=st.session_state.phase,
        messages=st.session_state.messages,
        conditions=st.session_state.target_conditions,
        stats=st.session_state.target_result_stats,
        member_ids=member_ids,
        reasoning=st.session_state.target_reasoning,
        push_copy=st.session_state.push_copy_result,
    )

def _render_target_card():
    """확정된 타겟 결과 카드(대상자 수 / 엑셀 다운로드 / 카피 작성 버튼)를 그린다.
    🌟 [UX 고도화] 이 카드를 메시지 목록 뒤에 고정으로 그리지 않고, 타겟이 확정된
    바로 그 메시지 자리에 끼워 넣어(render_chat_app 참고), 이후 이어지는 카피 대화가
    이 카드 "아래로" 자연스럽게 쌓이도록 한다."""
    if st.session_state.target_result_df is None:
        return
    with st.container(border=True):
        stats = st.session_state.target_result_stats or {}
        m1, m2 = st.columns(2)
        m1.metric("확정된 대상자 수", f"{stats.get('대상자수', 0):,}명")
        total_n = stats.get('전체시청자수', 0)
        share = f"{stats.get('대상자수', 0) / total_n * 100:.1f}%" if total_n else "-"
        m2.metric("전체 시청자 대비 비중", share)
        if stats.get('전체평균시청유지율'):
            st.caption(
                f"📊 이 타겟의 평균 시청유지율 {stats.get('평균시청유지율', 0)}% "
                f"(전체 평균 {stats.get('전체평균시청유지율', 0)}%) · "
                f"평균 총시청시간 {stats.get('평균총시청시간(분)', 0)}분 "
                f"(전체 평균 {stats.get('전체평균총시청시간(분)', 0)}분)"
            )
        show_cols = [c for c in ['R고객번호', '성별', '나이', '시청자SO', '선호장르', '선호시청시간대',
                                  '시청콘텐츠수', '평균시청유지율']
                     if c in st.session_state.target_result_df.columns]
        with st.expander("대상자 상세 보기"):
            st.dataframe(st.session_state.target_result_df[show_cols], width='stretch')

        with st.container(border=True):
            st.caption("R고객번호 엑셀 다운로드")
            st.download_button(
                "📥 R고객번호 엑셀 다운로드 (캠페인 업로드 양식)",
                data=_build_customer_id_excel(st.session_state.target_result_df),
                file_name="레인보우TV_이웃고객관리목록.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width='stretch',
            )

        if st.session_state.phase == 'targeting':
            if st.button("📝 이 타겟으로 카피 작성 시작"):
                st.session_state.pending_copy_start = True
                st.rerun()
        else:
            st.caption("아래 채팅창에 원하는 방향을 입력하면 카피를 다시 다듬어드립니다. (예: 더 친근하게, 이벤트 느낌으로)")

def _render_confirm_bar(profile_df, db_audience):
    """'이 조건으로 타겟 확정하기' 안내문 + 버튼을 그린다.
    🌟 [UX 고도화] 이 액션 바를 항상 화면 맨 아래(카드보다도 아래)에 고정으로 그리면,
    한 번 타겟을 확정한 뒤에는 버튼이 카드 밑에 다시 나타나 어색해 보인다. 그래서
    이미 확정된 타겟(카드)이 있을 때는 이 함수를 카드 "바로 위"(anchor 위치)에서
    호출하고, 아직 한 번도 확정한 적 없을 때만 메시지 목록 맨 끝에서 호출한다
    (render_chat_app 참고). 조건을 더 이야기해서 바꾼 뒤 다시 눌러 재확정하는 것도
    이 함수 하나로 그대로 지원된다."""
    st.info(f"🔖 현재까지 파악된 조건: {format_conditions_line(st.session_state.target_conditions)}")
    if st.button("✅ 이 조건으로 타겟 확정하기", type="primary"):
        with st.spinner("타겟을 계산하고 근거를 정리하는 중..."):
            target_df, stats = apply_target_conditions(profile_df, st.session_state.target_conditions, db_audience)
            summary_str = format_target_summary(st.session_state.target_conditions, stats)
            reasoning = generate_target_reasoning(str(st.session_state.target_conditions), str(stats))
        st.session_state.target_result_df = target_df
        st.session_state.target_result_stats = stats
        st.session_state.target_summary_str = summary_str
        st.session_state.target_reasoning = reasoning
        st.session_state.messages.append({
            "role": "assistant",
            "text": f"총 {stats.get('대상자수', 0)}명이 이 조건에 해당합니다.\n\n{reasoning}",
        })
        st.session_state.stream_next = True
        _autosave()
        st.rerun()


def render_chat_app(profile_df, db_audience=None):
    _inject_chat_css()

    # 다른 대화로 방금 전환한 경우: 저장된 대상자 스냅샷을 현재 프로필에 다시 매칭
    if '_pending_member_ids' in st.session_state:
        member_ids = set(st.session_state.pop('_pending_member_ids'))
        if member_ids and profile_df is not None and not profile_df.empty:
            st.session_state.target_result_df = profile_df[profile_df['R고객번호'].astype(str).isin(member_ids)]
        else:
            st.session_state.target_result_df = None
        st.session_state.target_summary_str = format_target_summary(
            st.session_state.target_conditions, st.session_state.target_result_stats
        )

    st.title("레T-고객 타겟팅&카소 자동화")
    st.caption("시청 데이터를 바탕으로 AI와 대화하며 앱푸시 타겟을 정하고, 근거를 확인하고, 카피까지 작성하세요.")
    if st.session_state.get('period_start') or st.session_state.get('period_end'):
        st.caption(f"📅 적용 중인 시청기간: {st.session_state.get('period_start')} ~ {st.session_state.get('period_end')}")

    if not st.session_state.messages:
        greeting = (
            "안녕하세요! 어떤 시청자분들께 앱푸시를 보내고 싶으신가요? "
            "예를 들어 '4050 여성 중 운동 콘텐츠 좋아하는 분들' 처럼 편하게 말씀해주세요."
        )
        _render_message("assistant", greeting, animate=st.session_state.get('greet_stream_pending', False))
        st.session_state.greet_stream_pending = False

    anchor_idx = None
    for i, turn in enumerate(st.session_state.messages):
        if turn["role"] == "assistant" and turn["text"].startswith("총 ") and "이 조건에 해당합니다" in turn["text"]:
            anchor_idx = i

    last_idx = len(st.session_state.messages) - 1
    for i, turn in enumerate(st.session_state.messages):
        should_animate = (i == last_idx and turn["role"] == "assistant" and st.session_state.get('stream_next'))
        _render_message(turn["role"], turn["text"], animate=should_animate)
        if should_animate:
            st.session_state.stream_next = False
        if i == anchor_idx:
            if st.session_state.target_conditions and st.session_state.phase == 'targeting':
                _render_confirm_bar(profile_df, db_audience)
            _render_target_card()

    if st.session_state.get('pending_copy_start'):
        st.session_state.pending_copy_start = False
        with st.spinner("카피 초안을 작성하는 중..."):
            new_messages, copy_text = process_copy_turn(
                st.session_state.messages, st.session_state.target_summary_str,
                st.session_state.target_reasoning, "",
            )
        st.session_state.messages = new_messages
        st.session_state.push_copy_result = copy_text
        st.session_state.phase = 'copywriting'
        st.session_state.stream_next = True
        _autosave()
        st.rerun()

    if st.session_state.get('pending_user_text'):
        pending = st.session_state.pop('pending_user_text')        
        with st.spinner("생각하는 중..."):
            if st.session_state.phase == 'copywriting':
                new_messages, copy_text = process_copy_turn(
                    st.session_state.messages[:-1], st.session_state.target_summary_str,
                    st.session_state.target_reasoning, pending,
                )
                st.session_state.messages = new_messages
                st.session_state.push_copy_result = copy_text
            else:
                new_messages, new_conditions = process_target_turn(
                    st.session_state.messages[:-1], st.session_state.target_conditions, pending, profile_df,
                    db_audience,
                )
                st.session_state.messages = new_messages
                st.session_state.target_conditions = new_conditions
        st.session_state.stream_next = True
        _autosave()
        st.rerun()

    # ---- 조건 확정 액션 바 (아직 한 번도 타겟을 확정한 적 없는 경우에만 메시지 맨 끝에 표시.
    #      한 번이라도 확정한 뒤에는 위 메시지 루프에서 카드 바로 위에 표시된다) ----
    if anchor_idx is None and st.session_state.target_conditions and st.session_state.phase == 'targeting':
        _render_confirm_bar(profile_df, db_audience)

    # ---- 확정된 타겟 결과 카드 (위 메시지 루프에서 자리를 못 찾은 경우의 폴백) ----
    if anchor_idx is None and st.session_state.target_result_df is not None:
        _render_target_card()

    # ---- 채팅 입력 (phase에 따라 자동 분기) ----
    placeholder = (
        "예: 더 유머러스하게 / 이벤트 느낌으로 바꿔줘"
        if st.session_state.phase == 'copywriting'
        else "예: 4050 여성 중 운동 콘텐츠 좋아하는 분들 뽑아줘"
    )
    user_text = st.chat_input(placeholder)
    if user_text:
        # 🌟 [응답 순서 개선] 여기서는 사용자 메시지만 추가하고 즉시 rerun한다.
        # AI 호출은 위쪽의 pending_user_text 처리 블록에서 다음 rerun 때 수행되므로,
        # 사용자 말풍선이 "생각하는 중" 스피너보다 먼저 화면에 나타난다.
        st.session_state.messages.append({"role": "user", "text": user_text})
        st.session_state.pending_user_text = user_text
        st.rerun()