# ui/chat_app.py
# ============================================================
# 🌟 [모듈화] 예전에는 CSS/말풍선 렌더링, 순수 대화 처리 로직, 타겟 카드
# 렌더링까지 이 파일 하나에 전부 들어있었다(약 330줄). 파일이 길어질수록
# 한 곳의 실수가 전체 화면에 영향을 줄 위험이 커진다고 판단해 아래로 분리했다.
#   - ui/chat_styles.py           : CSS 주입, 말풍선/타이핑 애니메이션 렌더링
#   - ui/target_card.py           : 타겟 확정 안내문/버튼, 확정 결과 카드, 자동저장
#   - services/target_service.py  : 타겟 설정 대화 한 턴 처리 (순수 로직)
#   - services/copy_service.py    : 카피 작성 대화 한 턴 처리 (순수 로직)
# 이 파일에는 이제 화면 전체를 조립하는 render_chat_app()만 남는다.
# ============================================================
import streamlit as st
from database.db_manager import format_target_summary
from services.target_service import process_target_turn
from services.copy_service import process_copy_turn
from ui.chat_styles import inject_chat_css, render_message
from ui.target_card import render_target_card, render_confirm_bar, autosave


def render_chat_app(profile_df, db_audience=None):
    inject_chat_css()

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
        render_message("assistant", greeting, animate=st.session_state.get('greet_stream_pending', False))
        st.session_state.greet_stream_pending = False

    anchor_idx = None
    for i, turn in enumerate(st.session_state.messages):
        if turn["role"] == "assistant" and turn["text"].startswith("총 ") and "이 조건에 해당합니다" in turn["text"]:
            anchor_idx = i

    last_idx = len(st.session_state.messages) - 1
    for i, turn in enumerate(st.session_state.messages):
        should_animate = (i == last_idx and turn["role"] == "assistant" and st.session_state.get('stream_next'))
        render_message(turn["role"], turn["text"], animate=should_animate)
        if should_animate:
            st.session_state.stream_next = False
        if i == anchor_idx:
            if st.session_state.target_conditions and st.session_state.phase == 'targeting':
                render_confirm_bar(profile_df, db_audience)
            render_target_card()

    if st.session_state.get('pending_copy_start'):
        copy_type = st.session_state.pending_copy_start
        st.session_state.pending_copy_start = False
        spinner_text = "앱푸시 카피 초안을 작성하는 중..." if copy_type == 'push' else "SMS 문자 초안을 작성하는 중..."
        with st.spinner(spinner_text):
            new_messages, copy_text = process_copy_turn(
                st.session_state.messages, st.session_state.target_summary_str,
                st.session_state.target_reasoning, "", copy_type=copy_type,
            )
        st.session_state.messages = new_messages
        st.session_state.push_copy_result = copy_text
        st.session_state.last_copy_type = copy_type
        if copy_type == 'push':
            st.session_state.push_copy_generated = True
        else:
            st.session_state.sms_copy_generated = True
        st.session_state.phase = 'copywriting'
        st.session_state.stream_next = True
        autosave()
        st.rerun()

    if st.session_state.get('pending_user_text'):
        pending = st.session_state.pop('pending_user_text')
        with st.spinner("생각하는 중..."):
            if st.session_state.phase == 'copywriting':
                new_messages, copy_text = process_copy_turn(
                    st.session_state.messages[:-1], st.session_state.target_summary_str,
                    st.session_state.target_reasoning, pending,
                    copy_type=st.session_state.get('last_copy_type', 'push'),
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
        autosave()
        st.rerun()

    # ---- 조건 확정 액션 바 (아직 한 번도 타겟을 확정한 적 없는 경우에만 메시지 맨 끝에 표시.
    #      한 번이라도 확정한 뒤에는 위 메시지 루프에서 카드 바로 위에 표시된다) ----
    if anchor_idx is None and st.session_state.target_conditions and st.session_state.phase == 'targeting':
        render_confirm_bar(profile_df, db_audience)

    # ---- 확정된 타겟 결과 카드 (위 메시지 루프에서 자리를 못 찾은 경우의 폴백) ----
    if anchor_idx is None and st.session_state.target_result_df is not None:
        render_target_card()

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
