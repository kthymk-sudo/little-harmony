# ui/report_chat.py
# ============================================================
# 📝 분석서&보고서 탭 화면. 📊 분석 탭과 같은 단순 챗 패턴(phase 없음)이지만,
# 결과가 "차트 하나"가 아니라 "트리 요약 + 피드백 텍스트"이고, 데이터를
# 요청했을 때만 차트/표가 추가로 붙는다.
# ============================================================
import streamlit as st
from database.db_manager import save_conversation, make_conversation_title
from services.report_service import process_report_turn
from ui.chart_render import build_pivot_chart_figure, pivot_table_df
from ui.chat_styles import inject_chat_css, render_message
from utils.emm_export import build_emm_bytes, parse_report_tree


def _autosave():
    first_user_text = next((m['text'] for m in st.session_state.report_messages if m['role'] == 'user'), "")
    save_conversation(
        conv_id=st.session_state.report_current_conversation_id,
        title=make_conversation_title(first_user_text, feature='report'),
        phase='', messages=st.session_state.report_messages,
        conditions={}, stats={}, member_ids=[], reasoning="", push_copy="",
        feature='report',
    )


def render_report_chat(profile_df, db_audience=None, db_content=None):
    inject_chat_css()

    st.title("📝 분석서&보고서")
    st.caption("업무 내용을 붙여넣으면 트리 요약+피드백으로 정리해드려요. '~도 같이 넣어줘'처럼 데이터 집계도 요청할 수 있어요.")

    if not st.session_state.report_messages:
        greeting = (
            "안녕하세요! 정리하고 싶은 업무 내용을 붙여넣어주세요. "
            "예를 들어 '이번 주 캠페인 진행 내용 정리해줘. SO별 시청자수도 같이 넣어줘'처럼요."
        )
        render_message("assistant", greeting, animate=st.session_state.get('report_greet_stream_pending', False))
        st.session_state.report_greet_stream_pending = False

    last_idx = len(st.session_state.report_messages) - 1
    for i, turn in enumerate(st.session_state.report_messages):
        should_animate = (i == last_idx and turn["role"] == "assistant" and st.session_state.get('report_stream_next'))
        render_message(turn["role"], turn["text"], animate=should_animate)
        if should_animate:
            st.session_state.report_stream_next = False
        chart_spec = turn.get("chart")
        try:
            fig = build_pivot_chart_figure(chart_spec)
        except Exception:  # 그래프 하나가 깨져도 대화 화면 전체가 멈추지 않게
            fig = None
            st.caption("⚠️ 이 그래프는 그리지 못했어요.")
        if fig is not None:
            st.plotly_chart(fig, width='stretch', key=f"report_chart_{i}")
            table_df = pivot_table_df(chart_spec.get("data") or {})
            if table_df is not None:
                with st.expander("표로 보기"):
                    st.dataframe(table_df, width='stretch')

        if turn["role"] == "assistant" and parse_report_tree(turn["text"]):
            try:
                emm_bytes = build_emm_bytes(turn["text"], f"보고서_{i}")
                st.download_button(
                    "📥 알마인드 파일로 다운로드 (.emm)", data=emm_bytes,
                    file_name=f"하모니플러스_보고서_{i}.emm",
                    mime="application/octet-stream", key=f"emm_dl_{i}",
                )
            except ValueError:
                pass

    if st.session_state.get('report_pending_user_text'):
        pending = st.session_state.pop('report_pending_user_text')
        with st.spinner("보고서를 정리하는 중..."):
            new_messages, _ = process_report_turn(
                st.session_state.report_messages[:-1], pending, profile_df, db_audience, db_content,
            )
            st.session_state.report_messages = new_messages
        st.session_state.report_stream_next = True
        _autosave()
        st.rerun()

    user_text = st.chat_input("업무 내용을 붙여넣어주세요")
    if user_text:
        st.session_state.report_messages.append({"role": "user", "text": user_text})
        st.session_state.report_pending_user_text = user_text
        st.rerun()
