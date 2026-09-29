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
from database.db_manager import save_conversation, make_conversation_title
from services.analysis_service import chart_spec_from
from services.code_analyst import build_tables, run_analyst_turn, FEEDBACK_REQUEST_TEXT
from ui.chart_render import build_pivot_chart_figure, pivot_table_df
from ui.chat_styles import inject_chat_css, render_message


def _autosave():
    first_user_text = next((m['text'] for m in st.session_state.analysis_messages if m['role'] == 'user'), "")
    if not save_conversation(
        conv_id=st.session_state.analysis_current_conversation_id,
        title=make_conversation_title(first_user_text, feature='analysis'),
        phase='', messages=st.session_state.analysis_messages,
        conditions={}, stats={}, member_ids=[], reasoning="", push_copy="",
        feature='analysis',
    ):
        st.session_state.save_failed = True  # main.py가 다음 화면에서 경고를 띄운다


def _render_steps(steps, checks):
    with st.expander(f"🧮 계산 과정 보기 ({len(steps)}단계)"):
        for s in steps:
            st.markdown(f"**{s['번호']}단계 · {s['설명']}**")
            st.code(s['code'], language='python')
            if s.get('error'):
                st.caption(f"⚠️ 오류(다음 단계에서 고침): {s['error']}")
            elif s.get('preview'):
                st.text(s['preview'][:2000])
        if checks:
            st.caption("🛡️ 답변 검증 기록 - 실행 결과에 없는 숫자나 지어낸 결과는 거부하고 다시 요청했어요")
            for c in checks:
                st.caption(f"· {c}")


def _ask(user_text, feedback=False):
    st.session_state.analysis_messages.append({"role": "user", "text": user_text})
    st.session_state.analysis_pending_user_text = user_text
    st.session_state.analysis_pending_feedback = feedback
    st.rerun()


def render_analysis_chat(profile_df, db_audience=None, db_content=None):
    inject_chat_css()

    st.title("📊 시청 데이터 분석")
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
        if turn.get("steps") or turn.get("checks"):
            _render_steps(turn.get("steps") or [], turn.get("checks") or [])
        chart_spec = turn.get("chart")
        try:
            fig = build_pivot_chart_figure(chart_spec)
        except Exception:  # 그래프 하나가 깨져도 대화 화면 전체가 멈추지 않게
            fig = None
            st.caption("⚠️ 이 그래프는 그리지 못했어요. 그래프 종류를 바꿔서 다시 요청해 주세요.")
        if fig is not None:
            st.plotly_chart(fig, width='stretch', key=f"analysis_chart_{i}")
            table_df = pivot_table_df(chart_spec.get("data") or {})
            if table_df is not None:
                with st.expander("표로 보기"):
                    st.dataframe(table_df, width='stretch')
        if turn.get("data"):
            # 그래프와 전체 비교 피드백은 실무자가 원할 때만 만든다
            chart_col, feedback_col, _ = st.columns([1, 1.4, 3])
            if fig is None and chart_col.button("📊 그래프로 보기", key=f"analysis_to_chart_{i}"):
                turn["chart"] = chart_spec_from(turn["data"])  # AI 재호출 없음
                _autosave()
                st.rerun()
            if feedback_col.button("🔍 전체와 비교 피드백", key=f"analysis_feedback_{i}"):
                _ask(FEEDBACK_REQUEST_TEXT, feedback=True)

    if st.session_state.get('analysis_pending_user_text'):
        pending = st.session_state.pop('analysis_pending_user_text')
        feedback = st.session_state.pop('analysis_pending_feedback', False) or '피드백' in pending
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
        _autosave()
        st.rerun()

    user_text = st.chat_input("예: 8월 SO별 MAU와 재방문율, 특이한 SO가 있으면 어떤 콘텐츠 때문인지도 봐줘")
    if user_text:
        _ask(user_text)
