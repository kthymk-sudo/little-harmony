# ui/analysis_chat.py
# ============================================================
# 📊 분석 탭 화면. 🎯 타겟팅&카피 탭(ui/chat_app.py)과 인터랙션 패턴은 같지만
# (자연어 요청 -> AI 파싱 -> 결과 렌더링), phase 상태머신이나 타겟 확정 카드 같은
# 타겟팅 전용 개념은 없다.
# 🌟 [대화형 분석] 매 턴 바로 차트를 그리던 흐름에서, 실제 숫자로 대화하며 분석하다가
# 실무자가 원할 때(말로 요청하거나 답변 아래 버튼) 그래프를 만드는 흐름으로 바뀌었다.
# ============================================================
import streamlit as st
from config import start_new_analysis_conversation
from database.db_manager import save_conversation, make_conversation_title
from services.analysis_service import process_analysis_turn, process_feedback_request, chart_spec_from
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
    st.caption("AI와 대화하며 실제 데이터로 분석하고, 필요할 때 그래프로 만들어요.")

    if not st.session_state.analysis_messages:
        greeting = (
            "안녕하세요! 시청 데이터에 대해 궁금한 걸 편하게 물어봐주세요. 실제 데이터를 집계해서 답해드릴게요. "
            "그래프나 전체 데이터와 비교한 피드백이 필요하면 말씀하시거나 답변 아래 버튼을 눌러주세요."
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
        if turn.get("data"):
            # 🌟 [대화형 분석] 그래프와 전체 비교 피드백은 실무자가 원할 때만 만든다
            chart_col, feedback_col, _ = st.columns([1, 1.4, 3])
            if fig is None and chart_col.button("📊 그래프로 보기", key=f"analysis_to_chart_{i}"):
                turn["chart"] = chart_spec_from(turn["data"])  # AI 재호출 없음
                _autosave()
                st.rerun()
            if feedback_col.button("🔍 전체와 비교 피드백", key=f"analysis_feedback_{i}"):
                st.session_state.analysis_pending_feedback = i
                st.rerun()

    if st.session_state.get('analysis_pending_feedback') is not None:
        index = st.session_state.pop('analysis_pending_feedback')
        with st.spinner("전체 데이터와 비교하는 중..."):
            st.session_state.analysis_messages = process_feedback_request(
                st.session_state.analysis_messages, index, profile_df, db_audience, db_content,
            )
        st.session_state.analysis_stream_next = True
        _autosave()
        st.rerun()

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

    user_text = st.chat_input("예: 요즘 시청이 가장 많은 시간대가 언제야?")
    if user_text:
        st.session_state.analysis_messages.append({"role": "user", "text": user_text})
        st.session_state.analysis_pending_user_text = user_text
        st.rerun()
