# ui/analysis_chat.py
# ============================================================
# 📊 분석 탭 화면. 🎯 타겟팅&카피 탭(ui/chat_app.py)과 인터랙션 패턴은 같지만
# (자연어 요청 -> AI 파싱 -> 결과 렌더링), phase 상태머신이나 타겟 확정 카드 같은
# 타겟팅 전용 개념은 없다 - 매 턴 "차트 하나 보여주기"로 끝나는 훨씬 단순한 흐름.
# ============================================================
import streamlit as st
from config import start_new_analysis_conversation
from database.db_manager import save_conversation, make_conversation_title
from services.analysis_service import process_analysis_turn
from ui.chart_render import build_pivot_chart_figure, pivot_table_df
from ui.chat_styles import inject_chat_css, render_message


def _autosave():
    first_user_text = next((m['text'] for m in st.session_state.analysis_messages if m['role'] == 'user'), "")
    save_conversation(
        conv_id=st.session_state.analysis_current_conversation_id,
        title=make_conversation_title(first_user_text, feature='analysis'),
        phase='', messages=st.session_state.analysis_messages,
        conditions={}, stats={}, member_ids=[], reasoning="", push_copy="",
        feature='analysis',
    )


def render_analysis_chat(profile_df, db_audience=None, db_content=None):
    inject_chat_css()

    st.title("📊 시청 데이터 분석")
    st.caption("궁금한 걸 자연어로 물어보면 AI가 알맞은 차트로 보여드려요. 예: 'SO별 시청자 분포 보여줘', '가장 인기있는 콘텐츠 TOP10'")

    if not st.session_state.analysis_messages:
        greeting = (
            "안녕하세요! 시청 데이터에 대해 궁금한 걸 물어봐주세요. "
            "예를 들어 '성별로 시청자 분포가 어때?' 또는 '가장 인기있는 채널 TOP10 보여줘'처럼요."
        )
        render_message("assistant", greeting, animate=st.session_state.get('analysis_greet_stream_pending', False))
        st.session_state.analysis_greet_stream_pending = False

    last_idx = len(st.session_state.analysis_messages) - 1
    for i, turn in enumerate(st.session_state.analysis_messages):
        should_animate = (i == last_idx and turn["role"] == "assistant" and st.session_state.get('analysis_stream_next'))
        render_message(turn["role"], turn["text"], animate=should_animate)
        if should_animate:
            st.session_state.analysis_stream_next = False
        chart_spec = turn.get("chart")
        fig = build_pivot_chart_figure(chart_spec)
        if fig is not None:
            st.plotly_chart(fig, width='stretch', key=f"analysis_chart_{i}")
            table_df = pivot_table_df(chart_spec.get("data") or {})
            if table_df is not None:
                with st.expander("표로 보기"):
                    st.dataframe(table_df, width='stretch')

    if st.session_state.get('analysis_pending_user_text'):
        pending = st.session_state.pop('analysis_pending_user_text')
        with st.spinner("분석하는 중..."):
            new_messages, _ = process_analysis_turn(
                st.session_state.analysis_messages[:-1], pending, profile_df, db_audience, db_content,
            )
            st.session_state.analysis_messages = new_messages
        st.session_state.analysis_stream_next = True
        _autosave()
        st.rerun()

    user_text = st.chat_input("예: SO별 시청자 분포 보여줘")
    if user_text:
        st.session_state.analysis_messages.append({"role": "user", "text": user_text})
        st.session_state.analysis_pending_user_text = user_text
        st.rerun()
