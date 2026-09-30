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
from database.db_manager import format_target_summary, save_conversation, make_conversation_title
from services.target_service import process_target_turn
from services.copy_service import process_copy_turn
from ui import job_runner
from ui.chat_styles import inject_chat_css, render_message
from ui.target_card import render_target_card, render_confirm_bar, autosave


def _snapshot():
    """백그라운드 작업 스레드에 넘길 현재 상태 복사본(작업 스레드는 st.session_state를 쓰면 안 된다)."""
    df = st.session_state.target_result_df
    return {
        'phase': st.session_state.phase, 'conditions': st.session_state.target_conditions,
        'stats': st.session_state.target_result_stats, 'reasoning': st.session_state.target_reasoning,
        'summary': st.session_state.target_summary_str, 'push_copy': st.session_state.push_copy_result,
        'last_copy_type': st.session_state.get('last_copy_type', 'push'),
        'push_generated': st.session_state.get('push_copy_generated', False),
        'sms_generated': st.session_state.get('sms_copy_generated', False),
        'member_ids': df['R고객번호'].astype(str).tolist() if df is not None else [],
    }


def _next_state(snap, result):
    """작업 결과를 반영한 다음 상태. 화면 반영과 백그라운드 저장이 같은 값을 쓰도록 한 곳에서 계산한다."""
    s = {**snap, 'messages': result['messages']}
    if result['kind'] == 'target':
        s['conditions'] = result['conditions']
    elif result['copy_text'] is not None:   # 🌟 생성에 실패하면(None) 카피로 저장하지 않고 직전 카피와 버튼 상태를 그대로 둔다
        s['push_copy'] = result['copy_text']
        if result['start']:
            s['last_copy_type'] = result['copy_type']
            s[f"{result['copy_type']}_generated"] = True
            s['phase'] = 'copywriting'
    return s


def _persist(conv_id, snap):
    def persist(result):
        s = _next_state(snap, result)
        first_user_text = next((m['text'] for m in s['messages'] if m['role'] == 'user'), "")
        save_conversation(
            conv_id=conv_id, title=make_conversation_title(first_user_text), phase=s['phase'], messages=s['messages'],
            conditions=s['conditions'], stats=s['stats'], member_ids=s['member_ids'], reasoning=s['reasoning'], push_copy=s['push_copy'],
        )
    return persist


def render_chat_app(profile_df, db_audience=None, db_content=None):
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

    st.title(":material/campaign: 타겟팅 & 카피")
    st.caption("시청 데이터를 바탕으로 AI와 대화하며 앱푸시 타겟을 정하고, 근거를 확인하고, 카피까지 작성하세요.")

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
            render_target_card()

    # 🌟 답변·카피 만들기는 백그라운드로 돈다 - 끝나기 전에 다른 탭·대화로 넘어가도 끊기지 않고 대화에 저장된다(ui/job_runner.py)
    conv_id = st.session_state.current_conversation_id
    copy_type, user_text_now = st.session_state.pop('pending_copy_start', False), None
    if not copy_type:
        user_text_now = st.session_state.pop('pending_user_text', None)
    if copy_type or user_text_now:
        snap, msgs = _snapshot(), list(st.session_state.messages)
        if copy_type:
            label = "앱푸시 카피 초안을 작성하는 중..." if copy_type == 'push' else "SMS 문자 초안을 작성하는 중..."

            def work(job):
                new_messages, copy_text = process_copy_turn(msgs, snap['summary'], snap['reasoning'], "", copy_type=copy_type)
                return {'kind': 'copy', 'start': True, 'copy_type': copy_type, 'messages': new_messages, 'copy_text': copy_text}
        elif snap['phase'] == 'copywriting':
            label = "생각하는 중..."

            def work(job):
                new_messages, copy_text = process_copy_turn(msgs[:-1], snap['summary'], snap['reasoning'], user_text_now,
                                                            copy_type=snap['last_copy_type'])
                return {'kind': 'copy', 'start': False, 'copy_type': snap['last_copy_type'], 'messages': new_messages, 'copy_text': copy_text}
        else:
            label = "생각하는 중..."

            def work(job):
                new_messages, new_conditions = process_target_turn(
                    msgs[:-1], snap['conditions'], user_text_now, profile_df, db_audience, db_content,
                )
                return {'kind': 'target', 'messages': new_messages, 'conditions': new_conditions}
        job_runner.start(conv_id, work, persist=_persist(conv_id, snap), label=label)

    outcome = job_runner.wait_for(conv_id)
    if outcome:
        status, value = outcome
        if status == "ok":
            new = _next_state(_snapshot(), value)
            st.session_state.messages = new['messages']
            st.session_state.target_conditions = new['conditions']
            st.session_state.push_copy_result = new['push_copy']
            st.session_state.last_copy_type = new['last_copy_type']
            st.session_state.push_copy_generated, st.session_state.sms_copy_generated = new['push_generated'], new['sms_generated']
            st.session_state.phase = new['phase']
        else:
            st.session_state.messages = st.session_state.messages + [
                {"role": "assistant", "text": f"⚠️ 답변을 만드는 중 오류가 생겼어요. 다시 시도해 주세요. ({type(value).__name__})"}]
        st.session_state.stream_next = True
        autosave()
        st.rerun()

    # ---- 조건 확정 액션 바 ----
    if st.session_state.target_conditions and st.session_state.phase == 'targeting':
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
