# ui/analysis_chat.py
# ============================================================
# 📊 분석 탭 화면. 🎯 타겟팅&카피 탭(ui/chat_app.py)과 인터랙션 패턴은 같지만
# (자연어 요청 -> AI 처리 -> 결과 렌더링), phase 상태머신이나 타겟 확정 카드 같은
# 타겟팅 전용 개념은 없다.
# 🌟 [대화형 분석] 실제 숫자로 대화하며 분석하다가, 실무자가 원할 때(말로 요청하거나
# 답변 아래 버튼) 그래프를 만든다.
# 🌟 [AI 코드 실행형] AI가 분석 코드를 직접 쓰고 시스템이 실행한다(services/code_analyst.py).
# 여러 단계로 파고드는 동안 진행 상황을 보여주고, 답변마다 "계산 과정 보기"로 실제 실행한
# 코드와 중간 결과를 확인할 수 있다.
# ============================================================
import streamlit as st
from config import start_new_conversation
from database.db_manager import (
    apply_target_conditions, format_target_summary,
)
from services.analysis_service import chart_spec_from
from services.report_service import build_material
from services.code_analyst import build_tables, run_analyst_turn, is_feedback_request, FEEDBACK_REQUEST_TEXT
from ui.chart_render import render_chart_card
from ui.chat_styles import inject_chat_css, render_message
from ui.target_card import autosave as autosave_targeting, autosave_simple


def _render_steps(steps, checks):
    with st.expander(f"계산 과정 보기 ({len(steps)}단계)", icon=":material/calculate:"):
        for s in steps:
            st.markdown(f"**{s['번호']}단계 · {s['설명']}**")
            st.code(s['code'], language='python')
            if s.get('error'):
                st.caption(f":material/warning: 오류(다음 단계에서 고침): {s['error']}")
            elif s.get('preview'):
                st.text(s['preview'][:2000])
        if checks:
            st.caption(":material/verified_user: 답변 검증 기록 - 실행 결과에 없는 숫자나 지어낸 결과는 거부하고 다시 요청했어요")
            for c in checks:
                st.caption(f"· {c}")


def _send_to_targeting(info, profile_df, db_audience):
    """🌟 [분석 → 타겟 연결] 분석에서 찾은 집단(고객번호 목록)을 새 타겟팅 대화의 확정된 타겟으로 만들어 넘긴다.
    타겟팅 탭에서는 바로 R고객번호 엑셀 다운로드/카피 작성을 할 수 있고, 채팅으로 조건을 더 좁힐 수도 있다."""
    conditions = {'분석출처': info['설명'], '__고객번호': info['ids']}
    target_df, stats = apply_target_conditions(profile_df, conditions, db_audience)
    start_new_conversation()
    reasoning = f"분석 탭에서 '{info['설명']}'(으)로 찾은 집단이에요."
    st.session_state.target_conditions = conditions
    st.session_state.target_result_df = target_df
    st.session_state.target_result_stats = stats
    st.session_state.target_summary_str = format_target_summary(conditions, stats)
    st.session_state.target_reasoning = reasoning
    st.session_state.messages = [
        {"role": "user", "text": f"[분석에서 가져옴] {info['설명']}"},
        {"role": "assistant", "text": f"총 {stats.get('대상자수', 0):,}명이 이 조건에 해당합니다.\n\n{reasoning} "
                                      "R고객번호 엑셀을 내려받거나 카피를 작성할 수 있어요. 조건을 더 좁히고 싶으면 채팅으로 말씀해 주세요."},
    ]
    st.session_state.greet_stream_pending = False
    st.session_state.active_feature = 'targeting'
    autosave_targeting()
    st.rerun()


def _send_to_report(i, turn):
    """🌟 [분석 → 보고서 연결] 이 분석 답변(글+표)을 현재 보고서 대화에 '분석 자료'로 담는다. 보고서 대화에서
    "취합안에 이 분석도 넣어줘"라고 하면 AI가 자료의 숫자를 그대로 옮겨 넣는다."""
    question = next((m['text'] for m in reversed(st.session_state.analysis_messages[:i]) if m['role'] == 'user'), "분석 결과")
    material = build_material(question, turn)
    st.session_state.report_messages += [
        {"role": "user", "text": f"[분석에서 가져옴] {material['제목']}"},
        {"role": "assistant", "material": material,
         "text": f"분석 자료를 담았어요: {material['제목']}. 취합안이나 보고서에 넣고 싶다고 말씀해 주세요(예: '이번 취합안에 이 분석도 넣어줘')."},
    ]
    st.session_state.report_greet_stream_pending = False
    st.session_state.active_feature = 'report'
    autosave_simple('report')
    st.rerun()


def _render_actions(i, turn, profile_df, db_audience):
    """답변 말풍선 바로 아래의 알약 모양 액션 버튼 줄(그래프 보기/접기, 전체와 비교 피드백,
    타겟팅으로 보내기, 보고서에 넣기). 그래프와 피드백은 실무자가 원할 때만 만든다. 모양은 ui/chat_styles.py의 hp-actions 스타일."""
    with st.container(horizontal=True, gap="small", key=f"hp-actions-{i}"):
        if turn.get("data"):
            showing = bool(turn.get("chart"))
            if st.button("그래프 접기" if showing else "그래프로 보기", width="content", key=f"analysis_to_chart_{i}",
                         icon=":material/expand_less:" if showing else ":material/bar_chart:"):
                if showing:
                    turn.pop("chart")
                else:
                    turn["chart"] = chart_spec_from(turn["data"])  # AI 재호출 없음
                autosave_simple('analysis')
                st.rerun()
            if st.button("전체와 비교 피드백", width="content", key=f"analysis_feedback_{i}", icon=":material/insights:"):
                _ask(FEEDBACK_REQUEST_TEXT, feedback=True)
        if turn.get("audience"):
            info = turn["audience"]
            if st.button(f"타겟팅으로 보내기 ({info['count']:,}명)", width="content", key=f"analysis_to_target_{i}",
                         icon=":material/send:"):
                _send_to_targeting(info, profile_df, db_audience)
        if turn.get("steps"):  # 실제로 계산한 분석 답변만 보고서 자료로 담을 수 있다
            if st.button("보고서에 넣기", width="content", key=f"analysis_to_report_{i}", icon=":material/description:"):
                _send_to_report(i, turn)


def _ask(user_text, feedback=False):
    st.session_state.analysis_messages.append({"role": "user", "text": user_text})
    st.session_state.analysis_pending_user_text = user_text
    st.session_state.analysis_pending_feedback = feedback
    st.rerun()


def render_analysis_chat(profile_df, db_audience=None, db_content=None):
    inject_chat_css()

    st.title(":material/analytics: 데이터 분석")
    st.caption("AI가 실제 데이터를 직접 계산하며 분석해요. 궁금한 걸 자유롭게 물어보고, 필요할 때 그래프로 만들어요.")

    if not st.session_state.analysis_messages:
        greeting = (
            "안녕하세요! 시청 데이터에 대해 궁금한 걸 자유롭게 물어봐주세요. 필요한 계산을 단계별로 직접 해서 답해드릴게요. "
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
        if turn.get("data") or turn.get("table") or turn.get("audience") or turn.get("steps"):
            _render_actions(i, turn, profile_df, db_audience)
        render_chart_card(turn.get("chart"), f"analysis_chart_{i}")
        if turn.get("steps") or turn.get("checks"):
            _render_steps(turn.get("steps") or [], turn.get("checks") or [])

    if st.session_state.get('analysis_pending_user_text'):
        pending = st.session_state.pop('analysis_pending_user_text')
        feedback = st.session_state.pop('analysis_pending_feedback', False) or is_feedback_request(pending)
        with st.status("분석을 시작하는 중...", expanded=False) as status:
            def _on_step(number, description):
                status.update(label=f"{number}단계 계산 중 · {description}")

            try:
                tables = build_tables(db_audience, db_content, profile_df)
                reply = run_analyst_turn(
                    st.session_state.analysis_messages[:-1], pending, tables, on_step=_on_step, feedback=feedback,
                )
                status.update(label="분석 완료", state="complete")
            except Exception as e:  # 예상 못한 오류가 나도 질문에 답이 남도록(화면 전체가 멈추지 않게)
                reply = {"role": "assistant", "text": f"⚠️ 분석 중 오류가 생겼어요. 질문을 조금 바꿔서 다시 시도해주세요. ({type(e).__name__})"}
                status.update(label="분석 중 오류", state="error")
        st.session_state.analysis_messages.append(reply)
        st.session_state.analysis_stream_next = True
        autosave_simple('analysis')
        st.rerun()

    user_text = st.chat_input("예: 8월 SO별 MAU와 재방문율, 특이한 SO가 있으면 어떤 콘텐츠 때문인지도 봐줘")
    if user_text:
        _ask(user_text)
