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
    raw = generate_ai_push_copy(target_profile_str, reasoning_str, extra_request_str, channel_type)
    return format_push_copy_for_display(raw)


def render_push_copy_panel():
    st.subheader("3. 마케팅 카피 작성")

    # 🌟 1. 매체 상태 관리
    if "current_channel" not in st.session_state:
        st.session_state.current_channel = "app_push"

    # 🌟 2. 매체 선택 패널(버튼 2개)을 조건문보다 무조건 '위'로 끌어올렸습니다!
    # 이제 타겟을 확정하지 않았더라도 버튼 두 개는 무조건 화면에 나타납니다.
    st.caption("발송할 매체를 선택하면 AI가 맞춤형 카피를 작성합니다.")
    col1, col2 = st.columns(2)
    
    should_generate = False
    trigger_text = ""
    user_clicked_button = False

    with col1:
        if st.button("📱 앱푸시 (짧고 강력하게)", use_container_width=True, 
                     type="primary" if st.session_state.current_channel == "app_push" else "secondary"):
            st.session_state.current_channel = "app_push"
            user_clicked_button = True
            trigger_text = "앱푸시 형식(짧고 강력하게)으로 작성해 줘."

    with col2:
        if st.button("✉️ 문자 (상세하고 여유있게)", use_container_width=True, 
                     type="primary" if st.session_state.current_channel == "sms" else "secondary"):
            st.session_state.current_channel = "sms"
            user_clicked_button = True
            trigger_text = "문자 메시지 형식(상세하게)으로 작성해 줘."

    # 🌟 3. 타겟 확정 여부 방어 로직 (버튼을 그린 '이후'로 위치 이동)
    # 안전하게 .get()을 사용하여 에러를 방지합니다.
    if st.session_state.get("target_result_df") is None:
        st.info("💡 카피를 생성하려면 먼저 2단계에서 타겟을 확정해 주세요.")
        return

    # 버튼 클릭 여부를 카피 생성 트리거로 연결
    should_generate = user_clicked_button

    # 4. 기존 채팅 내역 출력
    for turn in st.session_state.push_chat_history:
        role = "user" if turn["role"] == "user" else "assistant"
        with st.chat_message(role):
            st.write(turn["text"])

    # 5. 채팅 입력창
    user_text = st.chat_input("예: 더 친근한 느낌으로 / 이벤트 느낌 강조해줘")
    
    if user_text:
        should_generate = True
        trigger_text = user_text
    elif not st.session_state.push_chat_history and not should_generate:
        # 최초 접속 시 자동 생성 (기본 앱푸시)
        should_generate = True
        trigger_text = "타겟 맞춤형 카피를 작성해 줘."

    # 6. 생성 실행
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

    # 7. 하단 R고객번호 복사 영역
    if st.session_state.get("push_copy_result"):
        st.divider()
        st.write("📋 CRM 붙여넣기용 대상자 R고객번호 목록")
        id_list = "\n".join(st.session_state.target_result_df['R고객번호'].astype(str).tolist())
        st.text_area("R고객번호 목록 (복사해서 CRM에 붙여넣기)", id_list, height=150)