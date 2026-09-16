# ui/push_copy_panel.py
# ============================================================
# 기능 3: 확정된 타겟에게 보낼 앱푸시 카피를 AI와 대화하며 작성하는 화면.
# 최종적으로 CRM에 붙여넣을 R고객번호 목록도 함께 제공한다.
# ============================================================
import streamlit as st
from ai_engine.gemini_api import generate_ai_push_copy
from utils.response_parser import format_push_copy_for_display
from database.db_manager import update_target_history_copy


def generate_push_copy(target_profile_str, reasoning_str, extra_request_str=""):
    """카피 생성 순수 처리 로직 (단위 테스트 가능)."""
    raw = generate_ai_push_copy(target_profile_str, reasoning_str, extra_request_str)
    return format_push_copy_for_display(raw)


def render_push_copy_panel():
    st.subheader("3. 앱푸시 카피 작성")

    if st.session_state.target_result_df is None:
        st.info("먼저 2단계에서 타겟을 확정해주세요.")
        return

    for turn in st.session_state.push_chat_history:
        role = "user" if turn["role"] == "user" else "assistant"
        with st.chat_message(role):
            st.write(turn["text"])

    user_text = st.chat_input("예: 더 친근한 느낌으로 / 이벤트 느낌 강조해줘")
    should_generate = bool(user_text) or not st.session_state.push_chat_history

    if should_generate:
        with st.spinner("카피를 생성하는 중..."):
            copy_result = generate_push_copy(
                st.session_state.target_summary_str,
                st.session_state.target_reasoning,
                user_text or "",
            )
        if user_text:
            st.session_state.push_chat_history.append({"role": "user", "text": user_text})
        st.session_state.push_chat_history.append({"role": "ai", "text": copy_result})
        st.session_state.push_copy_result = copy_result
        # 이 타겟이 이미 이력에 저장되어 있다면, 최신 카피로 함께 갱신
        if st.session_state.active_history_id:
            update_target_history_copy(st.session_state.active_history_id, copy_result)
        st.rerun()

    if st.session_state.push_copy_result:
        st.divider()
        st.write("📋 CRM 붙여넣기용 대상자 R고객번호 목록")
        id_list = "\n".join(st.session_state.target_result_df['R고객번호'].astype(str).tolist())
        st.text_area("R고객번호 목록 (복사해서 CRM에 붙여넣기)", id_list, height=150)