# ui/target_chat_panel.py
# ============================================================
# 기능 1: AI와 대화하듯 앱푸시 타겟을 설정하는 화면.
# 화면 로직(render_*)과 순수 처리 로직(process_user_turn)을 분리해서,
# process_user_turn은 Streamlit 없이도 단위 테스트할 수 있게 했다.
# ============================================================
import json
import streamlit as st
from ai_engine.gemini_api import generate_target_chat_reply
from database.db_manager import summarize_profile_context
from utils.response_parser import parse_target_conditions


def process_user_turn(chat_history, current_conditions, user_text, profile_df):
    """
    한 턴의 대화를 처리한다: AI 호출 -> 응답에서 조건 JSON 분리 -> 새로운 조건으로 덮어쓰기.
    """
    history_with_user = chat_history + [{"role": "user", "text": user_text}]
    profile_context_str = summarize_profile_context(profile_df)
    current_conditions_str = json.dumps(current_conditions or {}, ensure_ascii=False)

    # AI 호출
    ai_raw = generate_target_chat_reply(history_with_user, profile_context_str, current_conditions_str)
    reply_text, parsed_conditions = parse_target_conditions(ai_raw)

    # 🌟 [마법의 롤백 해결책] 
    # AI가 '전체 조건 상태'를 다시 빚어주므로, 빈 값(삭제된 조건)을 제외하고 완전히 새로 덮어씁니다.
    new_conditions = {}
    for k, v in (parsed_conditions or {}).items():
        # "질문조건"이나 "확인필요단어"는 확정 타겟이 아니므로 제외합니다.
        if k in ["질문조건", "확인필요단어"]:
            continue
            
        # 값이 비어있지 않은(유지되거나 추가된) 실제 조건들만 새 조건표에 담습니다.
        # AI가 빈 값으로 보낸(삭제한) 조건은 이 과정에서 자연스럽게 증발(롤백)합니다!
        if v not in (None, [], "", {}):
            new_conditions[k] = v

    new_history = history_with_user + [{"role": "ai", "text": reply_text}]
    
    return new_history, new_conditions


def render_target_chat_panel(profile_df):
    st.subheader("1. 타겟 대화형 설정")
    st.caption("자유롭게 원하는 타겟 조건을 이야기하거나, 막연하게 물어보면 AI가 먼저 후보를 제안합니다.")

    for turn in st.session_state.target_chat_history:
        role = "user" if turn["role"] == "user" else "assistant"
        with st.chat_message(role):
            st.write(turn["text"])

    if st.session_state.target_conditions:
        st.caption(f"🔖 현재까지 확정된 조건: {st.session_state.target_conditions}")

    user_text = st.chat_input("예: 4050 여성 중 운동 콘텐츠 좋아하는 분들 뽑아줘")
    if user_text:
        with st.spinner("AI가 답변을 준비하는 중..."):
            new_history, new_conditions = process_user_turn(
                st.session_state.target_chat_history,
                st.session_state.target_conditions,
                user_text,
                profile_df,
            )
        st.session_state.target_chat_history = new_history
        st.session_state.target_conditions = new_conditions
        st.rerun()
