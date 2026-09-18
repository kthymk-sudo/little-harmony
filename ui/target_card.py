# ui/target_card.py
# ============================================================
# 🌟 [모듈화] ui/chat_app.py에서 "타겟 확정 안내문 + 확정 버튼"과 "확정된 타겟
# 결과 카드(대상자 수/엑셀 다운로드/카피 작성 버튼)"를 그리는 부분만 분리했다.
# 대화 자동저장(autosave)도 이 카드들과 함께 호출되는 흐름이라 같이 옮겼다.
# ============================================================
import io
import streamlit as st
from ai_engine.gemini_api import generate_target_reasoning
from database.db_manager import (
    apply_target_conditions, format_target_summary, format_conditions_line,
    save_conversation, make_conversation_title,
)


def _build_customer_id_excel(df):
    """캠페인 업로드 양식(R-고객번호 단일 컬럼)에 맞춘 엑셀 바이트 생성."""
    buffer = io.BytesIO()
    export_df = df[['R고객번호']].astype(str).rename(columns={'R고객번호': 'R-고객번호'})
    export_df.to_excel(buffer, index=False, sheet_name='이웃고객관리목록')
    return buffer.getvalue()


def autosave():
    member_ids = (
        st.session_state.target_result_df['R고객번호'].astype(str).tolist()
        if st.session_state.target_result_df is not None else []
    )
    first_user_text = next((m['text'] for m in st.session_state.messages if m['role'] == 'user'), "")
    save_conversation(
        conv_id=st.session_state.current_conversation_id,
        title=make_conversation_title(first_user_text),
        phase=st.session_state.phase,
        messages=st.session_state.messages,
        conditions=st.session_state.target_conditions,
        stats=st.session_state.target_result_stats,
        member_ids=member_ids,
        reasoning=st.session_state.target_reasoning,
        push_copy=st.session_state.push_copy_result,
    )


def render_target_card():
    """확정된 타겟 결과 카드(대상자 수 / 엑셀 다운로드 / 카피 작성 버튼)를 그린다.
    🌟 [UX 고도화] 이 카드를 메시지 목록 뒤에 고정으로 그리지 않고, 타겟이 확정된
    바로 그 메시지 자리에 끼워 넣어(ui/chat_app.py 참고), 이후 이어지는 카피 대화가
    이 카드 "아래로" 자연스럽게 쌓이도록 한다."""
    if st.session_state.target_result_df is None:
        return
    with st.container(border=True):
        stats = st.session_state.target_result_stats or {}
        m1, m2 = st.columns(2)
        m1.metric("확정된 대상자 수", f"{stats.get('대상자수', 0):,}명")
        total_n = stats.get('전체시청자수', 0)
        share = f"{stats.get('대상자수', 0) / total_n * 100:.1f}%" if total_n else "-"
        m2.metric("전체 시청자 대비 비중", share)
        if stats.get('전체평균시청유지율'):
            st.caption(
                f"📊 이 타겟의 평균 시청유지율 {stats.get('평균시청유지율', 0)}% "
                f"(전체 평균 {stats.get('전체평균시청유지율', 0)}%) · "
                f"평균 총시청시간 {stats.get('평균총시청시간(분)', 0)}분 "
                f"(전체 평균 {stats.get('전체평균총시청시간(분)', 0)}분)"
            )
        show_cols = [c for c in ['R고객번호', '성별', '나이', '시청자SO', '선호장르', '선호시청시간대',
                                  '시청콘텐츠수', '평균시청유지율']
                     if c in st.session_state.target_result_df.columns]
        with st.expander("대상자 상세 보기"):
            st.dataframe(st.session_state.target_result_df[show_cols], width='stretch')

        with st.container(border=True):
            st.caption("R고객번호 엑셀 다운로드")
            st.download_button(
                "📥 R고객번호 엑셀 다운로드 (캠페인 업로드 양식)",
                data=_build_customer_id_excel(st.session_state.target_result_df),
                file_name="레인보우TV_이웃고객관리목록.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width='stretch',
            )

        col_push, col_sms = st.columns(2)
        with col_push:
            if st.button(
                "📱 앱푸시 카피 작성",
                disabled=st.session_state.get('push_copy_generated', False),
                width='stretch',
            ):
                st.session_state.pending_copy_start = 'push'
                st.rerun()
        with col_sms:
            if st.button(
                "✉️ SMS 문자 작성",
                disabled=st.session_state.get('sms_copy_generated', False),
                width='stretch',
            ):
                st.session_state.pending_copy_start = 'sms'
                st.rerun()

        if st.session_state.phase == 'copywriting':
            st.caption("아래 채팅창에 원하는 방향을 입력하면 방금 만든 카피를 다시 다듬어드립니다. (예: 더 친근하게, 이벤트 느낌으로)")


def render_confirm_bar(profile_df, db_audience):
    """'이 조건으로 타겟 확정하기' 안내문 + 버튼을 그린다.
    🌟 [UX 고도화] 이 액션 바를 항상 화면 맨 아래(카드보다도 아래)에 고정으로 그리면,
    한 번 타겟을 확정한 뒤에는 버튼이 카드 밑에 다시 나타나 어색해 보인다. 그래서
    이미 확정된 타겟(카드)이 있을 때는 이 함수를 카드 "바로 위"(anchor 위치)에서
    호출하고, 아직 한 번도 확정한 적 없을 때만 메시지 목록 맨 끝에서 호출한다
    (ui/chat_app.py 참고). 조건을 더 이야기해서 바꾼 뒤 다시 눌러 재확정하는 것도
    이 함수 하나로 그대로 지원된다."""
    st.info(f"🔖 현재까지 파악된 조건: {format_conditions_line(st.session_state.target_conditions)}")
    if st.button("✅ 이 조건으로 타겟 확정하기", type="primary"):
        with st.spinner("타겟을 계산하고 근거를 정리하는 중..."):
            target_df, stats = apply_target_conditions(profile_df, st.session_state.target_conditions, db_audience)
            summary_str = format_target_summary(st.session_state.target_conditions, stats)
            reasoning = generate_target_reasoning(str(st.session_state.target_conditions), str(stats))
        st.session_state.target_result_df = target_df
        st.session_state.target_result_stats = stats
        st.session_state.target_summary_str = summary_str
        st.session_state.target_reasoning = reasoning
        # 🌟 [카피 타입 분리] 새로 타겟을 확정할 때마다 앱푸시/SMS 버튼을 다시 눌러
        # 만들 수 있도록 두 생성 여부 플래그를 초기화한다.
        st.session_state.push_copy_generated = False
        st.session_state.sms_copy_generated = False
        st.session_state.messages.append({
            "role": "assistant",
            "text": f"총 {stats.get('대상자수', 0)}명이 이 조건에 해당합니다.\n\n{reasoning}",
        })
        st.session_state.stream_next = True
        autosave()
        st.rerun()
