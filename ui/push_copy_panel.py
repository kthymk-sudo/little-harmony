# ui/push_copy_panel.py
# ============================================================
# 기능 3: 확정된 타겟에게 보낼 앱푸시/문자 카피를 AI와 대화하며 작성하는 화면.
# 최종적으로 CRM에 붙여넣을 R고객번호 목록도 함께 제공한다.
# ============================================================
import streamlit as st
from ai_engine.gemini_api import generate_ai_push_copy
from utils.response_parser import format_push_copy_for_display
from database.db_manager import update_target_history_copy


def generate_push_copy(target_profile_str, reasoning_str, extra_request_str="", channel_type="app_push"):
    """🌟 카피 생성 순수 처리 로직 (매체 타입 파라미터 추가)"""
    # API 호출 시 channel_type을 함께 넘겨줍니다.
    raw = generate_ai_push_copy(target_profile_str, reasoning_str, extra_request_str, channel_type)
    return format_push_copy_for_display(raw)


def render_push_copy_panel():
    st.subheader("3. 마케팅 카피 작성")

    if st.session_state.target_result_df is None:
        st.info("먼저 2단계에서 타겟을 확정해주세요.")
        return

    # 🌟 현재 선택된 매체 상태 관리 (기본값: 앱푸시)
    if "current_channel" not in st.session_state:
        st.session_state.current_channel = "app_push"

    # 🌟 매체 선택 버튼을 나란히 배치 (선택된 매체는 파란색(primary)으로 강조)
    st.caption("발송할 매체를 선택하면 AI가 맞춤형 카피를 작성합니다.")
    col1, col2 = st.columns(2)
    
    should_generate = False
    trigger_text = ""

    with col1:
        if st.button("📱 앱푸시 (짧고 강력하게)", use_container_width=True, 
                     type="primary" if st.session_state.current_channel == "app_push" else "secondary"):
            st.session_state.current_channel = "app_push"
            should_generate = True
            trigger_text = "앱푸시 형식(짧고 강력하게)으로 작성해 줘."

    with col2:
        if st.button("✉️ 문자 (상세하고 여유있게)", use_container_width=True, 
                     type="primary" if st.session_state.current_channel == "sms" else "secondary"):
            st.session_state.current_channel = "sms"
            should_generate = True
            trigger_text = "문자 메시지 형식(상세하게)으로 작성해 줘."

    # 기존 채팅 내역 출력
    for turn in st.session_state.push_chat_history:
        role = "user" if turn["role"] == "user" else "assistant"
        with st.chat_message(role):
            st.write(turn["text"])

    # 채팅 입력창
    user_text = st.chat_input("예: 더 친근한 느낌으로 / 이벤트 느낌 강조해줘")
    
    if user_text:
        should_generate = True
        trigger_text = user_text
    elif not st.session_state.push_chat_history and not should_generate:
        # 최초 접속 시 자동 생성 (기본 앱푸시)
        should_generate = True
        trigger_text = "타겟 맞춤형 카피를 작성해 줘."

    # 🌟 생성 실행
    if should_generate:
        channel_label = "앱푸시" if st.session_state.current_channel == "app_push" else "문자"
        with st.spinner(f"{channel_label} 카피를 생성하는 중..."):
            copy_result = generate_push_copy(
                st.session_state.target_summary_str,
                st.session_state.target_reasoning,
                trigger_text,
                st.session_state.current_channel  # 🌟 AI에게 매체 정보 전달
            )
        
        if trigger_text:
            st.session_state.push_chat_history.append({"role": "user", "text": trigger_text})
        st.session_state.push_chat_history.append({"role": "ai", "text": copy_result})
        st.session_state.push_copy_result = copy_result
        
        if getattr(st.session_state, 'active_history_id', None):
            update_target_history_copy(st.session_state.active_history_id, copy_result)
        st.rerun()

    # 하단 R고객번호 복사 영역
    if st.session_state.get("push_copy_result"):
        st.divider()
        st.write("📋 CRM 붙여넣기용 대상자 R고객번호 목록")
        id_list = "\n".join(st.session_state.target_result_df['R고객번호'].astype(str).tolist())
        st.text_area("R고객번호 목록 (복사해서 CRM에 붙여넣기)", id_list, height=150)